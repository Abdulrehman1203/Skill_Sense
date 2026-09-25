"""Independent audio/video completion using a shared parent-row lock.

Phase 8 disconnect must call mark_video_ended via database_sync_to_async.
ended_at belongs to video; audio_ended_at belongs exclusively to Retell.
"""
from django.db import transaction
from django.utils import timezone

from .models import Interview, InterviewSession


def finish_if_ready(interview, session):
    # Null Retell means start-session deliberately degraded: video alone can end it.
    if (interview.status == Interview.Status.LIVE and session.ended_at is not None
            and (session.audio_ended_at is not None or interview.retell_session_id is None)):
        interview.status = Interview.Status.DONE
        interview.save(update_fields=['status', 'updated_at'])


def mark_video_ended(session_id):
    with transaction.atomic():
        interview_id = InterviewSession.objects.values_list('interview_id', flat=True).get(pk=session_id)
        interview = Interview.objects.select_for_update().get(pk=interview_id)
        session = InterviewSession.objects.select_for_update().get(pk=session_id)
        if session.ended_at is None:
            session.ended_at = timezone.now()
            session.save(update_fields=['ended_at'])
        finish_if_ready(interview, session)
