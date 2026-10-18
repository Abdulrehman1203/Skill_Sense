"""Retell lifecycle and durable event ingestion shared by HTTP and workers."""
from datetime import datetime, timezone as datetime_timezone

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .models import Interview, InterviewSession, RetellCall, RetellWebhookEvent
from .session_completion import finish_if_ready


def normalize_call(call):
    if not isinstance(call, dict):
        raise serializers.ValidationError('Invalid call.')
    call_id = call.get('call_id')
    if not isinstance(call_id, str) or not call_id.strip() or len(call_id) > 255:
        raise serializers.ValidationError('Invalid call ID.')
    timestamp = call.get('end_timestamp')
    if timestamp is None and call.get('call_status') in ('error', 'not_connected'):
        # Unconnected calls may have no media timestamps; record receipt time.
        timestamp = timezone.now().timestamp() * 1000
    if type(timestamp) not in (int, float):
        raise serializers.ValidationError('Invalid end timestamp.')
    try:
        ended = datetime.fromtimestamp(timestamp / 1000, datetime_timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        raise serializers.ValidationError('Invalid end timestamp.') from None
    transcript = call.get('transcript', '')
    if transcript is None:
        transcript = ''  # Failed/unconnected calls may have no transcript.
    if not isinstance(transcript, str):
        raise serializers.ValidationError('Invalid transcript.')
    return {
        'retell_session_id': call_id, 'transcript': transcript, 'ended_at': ended,
        'status': call.get('call_status') if call.get('call_status') in ('ended', 'error', 'not_connected') else 'ended',
        'disconnection_reason': str(call.get('disconnection_reason') or '')[:255],
        'transcript_object': call.get('transcript_object') if isinstance(call.get('transcript_object'), list) else [],
        'recording_url': call.get('recording_url') if isinstance(call.get('recording_url'), str) else '',
        'analysis': call.get('call_analysis') if isinstance(call.get('call_analysis'), dict) else {},
    }


def accept_event(payload, event='call_ended'):
    row, _ = RetellWebhookEvent.objects.get_or_create(
        call_id=payload['retell_session_id'], event=event, defaults={'payload': payload},
    )
    if row.processed_at is None:
        transaction.on_commit(lambda: dispatch_event(row.pk))
    return row


def dispatch_event(event_id):
    from kombu.exceptions import OperationalError as BrokerError
    from .tasks.retell import process_retell_event
    try:
        process_retell_event.apply_async(args=[event_id], retry=False)
    except (BrokerError, OSError):
        # The inbox row is committed; Beat or recover_retell can dispatch it.
        import logging
        logging.getLogger(__name__).warning('Retell event saved; queue unavailable. event_id=%s', event_id)


def reconcile_creation(interview_id):
    from .integrations import retell_client
    calls = retell_client.find_interview_calls(interview_id)
    known = set(RetellCall.objects.filter(session__interview_id=interview_id).values_list('call_id', flat=True))
    calls = [call for call in calls if call['call_id'] not in known]
    if len(calls) != 1:
        # No match may be eventual consistency; never infer permission to create.
        return False
    data = calls[0]
    with transaction.atomic():
        interview = Interview.objects.select_for_update().get(pk=interview_id)
        session = InterviewSession.objects.select_for_update().get(interview=interview)
        if not session.creation_uncertain:
            return True
        RetellCall.objects.get_or_create(call_id=data['call_id'], defaults={'session': session, 'expires_at': 0})
        interview.retell_session_id = data['call_id']
        interview.status = Interview.Status.LIVE
        interview.save(update_fields=['retell_session_id', 'status', 'updated_at'])
        session.creation_uncertain = False
        session.started_at = session.started_at or timezone.now()
        session.save(update_fields=['creation_uncertain', 'started_at'])
        from .video_lifecycle import initialize_video_locked
        initialize_video_locked(session)
        if data.get('call_status') in ('ended', 'error', 'not_connected'):
            accept_event(normalize_call(data))
    return True


def apply_event(event_id):
    from django.utils.dateparse import parse_datetime
    with transaction.atomic():
        # All lifecycle writers lock parent -> session -> inbox to avoid inversion.
        ref = RetellWebhookEvent.objects.get(pk=event_id)
        matches = list(Interview.objects.select_for_update().filter(retell_session_id=ref.call_id)[:2])
        if len(matches) > 1:
            raise LookupError('Call is linked to multiple interviews.')
        if not matches:
            # Retired attempts are retained on RetellCall, but cannot finish the current attempt.
            if RetellCall.objects.filter(call_id=ref.call_id).exists():
                RetellWebhookEvent.objects.filter(pk=event_id).update(processed_at=timezone.now(), payload={})
                return 'superseded'
            raise LookupError('Call not yet linked.')
        interview = matches[0]
        session = InterviewSession.objects.select_for_update().get(interview=interview)
        row = RetellWebhookEvent.objects.select_for_update().get(pk=event_id)
        if row.processed_at is not None:
            return 'duplicate'
        payload = row.payload
        ended = parse_datetime(payload['ended_at'])
        if session.audio_ended_at is None:
            session.transcript = payload['transcript']
            session.audio_ended_at = ended
            session.save(update_fields=['transcript', 'audio_ended_at'])
        call = RetellCall.objects.filter(call_id=row.call_id).first()
        if call:
            call.status = payload.get('status', 'ended')
            call.ended_at = ended
            call.credentials = ''
            call.transcript = session.transcript
            call.disconnection_reason = payload.get('disconnection_reason', '')
            call.transcript_object = payload.get('transcript_object', []) or call.transcript_object
            call.recording_url = payload.get('recording_url', '') or call.recording_url
            call.analysis = payload.get('analysis', {}) or call.analysis
            call.save()
        if session.lifecycle_version == 1 and session.video_required:
            from .video_lifecycle import seal_video_locked
            seal_video_locked(interview, session, 'audio_ended')
        else:
            finish_if_ready(interview, session)
        row.processed_at = timezone.now()
        row.payload = {}
        row.last_error = ''
        row.save(update_fields=['processed_at', 'payload', 'last_error'])
    return 'processed'
