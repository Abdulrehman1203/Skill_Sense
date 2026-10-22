"""Durable business-event delivery. In-app once; SMTP is at-least-once."""
import uuid
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import get_connection, send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import NotificationOutbox
from ..notification_contract import EVENT_MESSAGES, event_name


@shared_task(ignore_result=True, acks_late=True, soft_time_limit=15, time_limit=20)
def deliver_notification_outbox(outbox_id):
    from ..notifications import deliver_inapp_locked, obsolete_question_notice
    token = uuid.uuid4()
    with transaction.atomic():
        row = NotificationOutbox.objects.select_for_update(of=('self',)).select_related('recipient').get(pk=outbox_id)
        if row.inapp_delivered_at is None and obsolete_question_notice(row):
            row.inapp_delivered_at, row.email_status = timezone.now(), 'SKIPPED'
            row.save()
            return {'status': 'SKIPPED', 'notification_id': str(row.pk)}
        deliver_inapp_locked(row)
        if row.event == 'feedback_released' and obsolete_question_notice(row):
            row.email_status, row.claim_token, row.lease_until = 'SKIPPED', None, None
            row.save()
            return {'status': 'SKIPPED', 'notification_id': str(row.pk)}
        if row.email_status in ('SENT', 'SKIPPED', 'FAILED'):
            return {'status': row.email_status, 'notification_id': str(row.pk)}
        if row.next_attempt_at > timezone.now() or (row.lease_until and row.lease_until > timezone.now()):
            return {'status': 'PENDING', 'notification_id': str(row.pk)}
        if row.email_attempts >= 4:
            row.email_status, row.error_code = 'FAILED', 'EMAIL_ATTEMPTS_EXHAUSTED'
            row.claim_token = row.lease_until = None
            row.save()
            return {'status': 'FAILED', 'notification_id': str(row.pk)}
        row.email_attempts += 1
        row.total_email_attempts += 1
        row.claim_token, row.lease_until = token, timezone.now() + timedelta(seconds=60)
        row.email_status = 'SENDING'
        row.save()
    try:
        subject, message = EVENT_MESSAGES[row.event]
        sent = send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [row.recipient.email],
            fail_silently=False, connection=get_connection(timeout=10))
        if sent != 1:
            raise RuntimeError('EMAIL_NOT_ACCEPTED')
    except Exception:
        NotificationOutbox.objects.filter(pk=row.pk, claim_token=token).update(
            email_status='FAILED' if row.email_attempts >= 4 else 'PENDING', error_code='EMAIL_UNAVAILABLE',
            claim_token=None, lease_until=None, next_attempt_at=timezone.now() + timedelta(seconds=15 * 2 ** row.email_attempts))
        return {'status': 'EMAIL_FAILED', 'notification_id': str(row.pk)}
    NotificationOutbox.objects.filter(pk=row.pk, claim_token=token).update(email_status='SENT', email_sent_at=timezone.now(),
        claim_token=None, lease_until=None, error_code='')
    return {'status': 'SENT', 'notification_id': str(row.pk)}


@shared_task(ignore_result=True)
def recover_notification_outbox():
    from ..notifications import dispatch_notification
    due = NotificationOutbox.objects.filter(Q(inapp_delivered_at__isnull=True) | Q(
        email_status__in=('PENDING', 'SENDING'), next_attempt_at__lte=timezone.now())).filter(
        Q(lease_until__isnull=True) | Q(lease_until__lte=timezone.now()))
    for pk in due.order_by('next_attempt_at').values_list('pk', flat=True)[:100]:
        dispatch_notification(pk)


@shared_task(name='interview_system.tasks.notifications.send_notification')
def send_notification(user_id, event):
    """Compatibility import/name. New worker requests must carry business identity.

    Old context-free messages cannot safely infer a business revision; fail closed.
    They never create another task-ID-derived notification.
    """
    from ..notifications import queue_notification
    event_name(event)
    if not isinstance(event, dict) or not all(key in event for key in ('resource_type', 'resource_id', 'revision')):
        raise ValueError('Notification requires resource_type, resource_id and revision')
    row = queue_notification(user_id, event, resource_type=event['resource_type'], resource_id=event['resource_id'],
        revision=event['revision'], links=event.get('links'), deliver_inapp=True)
    return deliver_notification_outbox.run(str(row.pk))
