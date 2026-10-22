"""Durable Retell processing and periodic recovery of missed deliveries."""
from datetime import timedelta

from celery import shared_task
from django.db import OperationalError, InterfaceError
from django.db.models import F, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from ..integrations import retell_client
from ..models import Interview, InterviewSession, RetellCall, RetellWebhookEvent
from ..voice import accept_event, apply_event, dispatch_event, normalize_call, reconcile_creation


@shared_task(bind=True, max_retries=5, ignore_result=True)
def process_retell_event(self, event_id):
    try:
        return apply_event(event_id)
    except (OperationalError, InterfaceError, LookupError):
        delay = min(300, 2 ** (self.request.retries + 1))
        RetellWebhookEvent.objects.filter(pk=event_id, processed_at__isnull=True).update(
            attempts=F('attempts') + 1,
            next_attempt_at=timezone.now() + timedelta(seconds=delay),
            last_error='processing_retry_required',
        )
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=delay) from None
        # Inbox remains pending and recoverable after Celery's retry budget.
        return 'pending_recovery'


@shared_task(ignore_result=True)
def recover_retell():
    from .conversation import recover_conversation_stops
    recover_conversation_stops.run()
    now = timezone.now()
    for interview_id in InterviewSession.objects.filter(creation_uncertain=True).values_list('interview_id', flat=True)[:25]:
        try:
            reconcile_creation(interview_id)
        except (retell_client.RetellUnavailableError, ValidationError):
            continue
    for event_id in RetellWebhookEvent.objects.filter(
        processed_at__isnull=True, next_attempt_at__lte=now,
    ).values_list('pk', flat=True)[:100]:
        RetellWebhookEvent.objects.filter(pk=event_id).update(next_attempt_at=now + timedelta(minutes=5))
        dispatch_event(event_id)
    # Recover calls whose terminal webhook never arrived. Never fetch arbitrary IDs.
    calls = RetellCall.objects.filter(
        session__interview__status=Interview.Status.LIVE, ended_at__isnull=True,
        created_at__lt=now - timedelta(minutes=2),
    ).filter(Q(last_checked_at__isnull=True) | Q(last_checked_at__lt=now - timedelta(minutes=2)))
    for call in calls.order_by('last_checked_at', 'created_at')[:25]:
        RetellCall.objects.filter(pk=call.pk).update(last_checked_at=now)
        try:
            data = retell_client.get_call(call.call_id)
            if data.get('call_status') in ('ended', 'error', 'not_connected'):
                accept_event(normalize_call(data), 'call_analyzed' if data.get('call_analysis') else 'call_ended')
        except (retell_client.RetellUnavailableError, ValidationError):
            continue
