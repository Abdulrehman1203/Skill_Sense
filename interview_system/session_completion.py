"""Independent audio/video completion using a shared parent-row lock.

Interview Analysis disconnect uses video_lifecycle.disconnect_publisher, never completion.
For video-required sessions ended_at records sealed video. Audio-only sessions use
the provider completion time for both ended_at and audio_ended_at.
"""
from django.db import transaction
from django.utils import timezone

from .models import Interview, InterviewSession


def finish_if_ready(interview, session):
    # Null Retell means start-session deliberately degraded: video alone can end it.
    if (interview.status == Interview.Status.LIVE
            and (session.ended_at is not None or not session.video_required)
            and (session.audio_ended_at is not None or interview.retell_session_id is None)):
        if not session.video_required and session.ended_at is None:
            session.ended_at = session.audio_ended_at or timezone.now()
            session.save(update_fields=['ended_at'])
        interview.status = Interview.Status.DONE
        interview.save(update_fields=['status', 'updated_at'])


def mark_video_ended(session_id):
    """Trusted explicit-finish compatibility entry point; never a socket callback."""
    with transaction.atomic():
        interview_id = InterviewSession.objects.values_list('interview_id', flat=True).get(pk=session_id)
        interview = Interview.objects.select_for_update().get(pk=interview_id)
        session = InterviewSession.objects.select_for_update().get(pk=session_id)
        if session.lifecycle_version == 1 and session.video_required:
            from .video_lifecycle import seal_video_locked
            seal_video_locked(interview, session, 'candidate_finished')
            return
        if session.ended_at is None:
            session.ended_at = timezone.now()
            session.save(update_fields=['ended_at'])
        finish_if_ready(interview, session)
