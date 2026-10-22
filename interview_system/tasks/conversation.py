"""Durable server-owned hang-up. Broker loss and send-before-ack are recoverable."""
from datetime import timedelta
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from ..models import InterviewConversation
from ..integrations import retell_client


@shared_task(ignore_result=True, acks_late=True)
def stop_conversation(interview_id):
    now = timezone.now()
    with transaction.atomic():
        row = InterviewConversation.objects.select_for_update().get(pk=interview_id)
        if (not row.stop_requested_at or row.stop_confirmed_at or row.next_stop_at > now
                or row.stop_attempts >= 8 or (row.stop_lease_until and row.stop_lease_until > now)):
            return
        row.stop_attempts += 1
        row.stop_lease_until = now + timedelta(seconds=30)
        row.save(update_fields=['stop_attempts', 'stop_lease_until'])
        call_id = row.call_id
    try:
        call = retell_client.get_call(call_id)
        if call.get('call_status') not in ('ended', 'error', 'not_connected'):
            retell_client.stop_call(call_id)
        # Stop API acknowledgement is not verified terminal media. Webhook/sync
        # still determines actual completion; retries first reconcile the call.
        InterviewConversation.objects.filter(pk=interview_id).update(stop_confirmed_at=timezone.now(), stop_lease_until=None)
    except retell_client.RetellUnavailableError:
        InterviewConversation.objects.filter(pk=interview_id).update(stop_lease_until=None,
            next_stop_at=timezone.now() + timedelta(seconds=min(120, 2 ** row.stop_attempts)))


@shared_task(ignore_result=True)
def recover_conversation_stops():
    from ..conversation_control import dispatch_stop
    for pk in InterviewConversation.objects.filter(stop_requested_at__isnull=False,
            stop_confirmed_at__isnull=True, stop_attempts__lt=8, next_stop_at__lte=timezone.now()).values_list('pk', flat=True)[:100]:
        dispatch_stop(pk)
