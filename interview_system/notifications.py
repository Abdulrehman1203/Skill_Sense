"""Business-keyed transactional outbox; broker publication is only a wake-up hint."""
import logging
import re
from uuid import NAMESPACE_URL, UUID, uuid5

from django.db import transaction
from django.utils import timezone

from .answer_evaluation import digest
from .models import Notification, NotificationOutbox, User
from .notification_contract import CANDIDATE_EVENTS, EVENT_MESSAGES, EVENT_RESOURCES, LINK_FIELDS, RESOURCE_TYPES, event_name

logger = logging.getLogger(__name__)


def deliver_inapp_locked(row):
    if row.inapp_delivered_at is None and obsolete_question_notice(row):
        row.inapp_delivered_at, row.email_status = timezone.now(), 'SKIPPED'
        row.save()
        return
    Notification.objects.get_or_create(pk=row.pk, defaults={'user_id': row.recipient_id,
        'type': row.event, 'message': EVENT_MESSAGES[row.event][1], 'links': row.links})
    if row.inapp_delivered_at is None:
        row.inapp_delivered_at = timezone.now()
        row.save(update_fields=['inapp_delivered_at'])


def obsolete_question_notice(row):
    if row.event == 'feedback_released':
        from .models import FeedbackReport
        return FeedbackReport.objects.filter(pk=row.resource_id, retracted_at__isnull=False).exists()
    if row.event != 'question_review_ready':
        return False
    from .models import QuestionSet
    current = QuestionSet.objects.filter(pk=row.resource_id).first()
    return not current or row.revision != f'generation:{current.generation_number}' or current.generation_status not in ('READY', 'FALLBACK')


@transaction.atomic
def queue_notification(user_id, event, *, resource_type, resource_id, revision, links=None, deliver_inapp=False, notification_id=None):
    name = event_name(event)
    recipient = User.objects.get(pk=user_id)
    expected_role = 'CANDIDATE' if name in CANDIDATE_EVENTS else 'RECRUITER'
    if recipient.role != expected_role or resource_type not in RESOURCE_TYPES or EVENT_RESOURCES[name] != resource_type:
        raise ValueError('Invalid notification recipient or resource')
    resource_id = UUID(str(resource_id))
    revision = str(revision)
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', revision):
        raise ValueError('Invalid business revision')
    links = links or {}
    if not isinstance(links, dict) or set(links) - LINK_FIELDS:
        raise ValueError('Invalid notification links')
    links = {key: str(UUID(str(value))) for key, value in links.items()}
    if links.get(f'{resource_type}_id', str(resource_id)) != str(resource_id):
        raise ValueError('Notification resource/link mismatch')
    links[f'{resource_type}_id'] = str(resource_id)
    key = digest({'recipient': str(recipient.pk), 'event': name, 'resource_type': resource_type,
                  'resource_id': str(resource_id), 'revision': revision})
    row, _ = NotificationOutbox.objects.get_or_create(business_key=key, defaults={
        'id': notification_id or uuid5(NAMESPACE_URL, 'skillsense-business-event:' + key),
        'recipient': recipient, 'event': name, 'resource_type': resource_type, 'resource_id': resource_id,
        'revision': revision, 'links': links})
    row = NotificationOutbox.objects.select_for_update().get(pk=row.pk)
    if row.links != links:
        raise ValueError('Business event cannot be reused with different links')
    if deliver_inapp:
        deliver_inapp_locked(row)
    if row.email_status in ('PENDING', 'SENDING') or row.inapp_delivered_at is None:
        transaction.on_commit(lambda: dispatch_notification(row.pk))
    return row


def dispatch_notification(pk):
    from .tasks.notification_tasks import deliver_notification_outbox
    try:
        # Bypass eager mode: request handlers never perform email I/O inline.
        deliver_notification_outbox.app.send_task(deliver_notification_outbox.name,
            args=[str(pk)], queue=deliver_notification_outbox.app.conf.task_default_queue)
    except Exception:
        logger.warning('Notification dispatch deferred outbox_id=%s', pk)
