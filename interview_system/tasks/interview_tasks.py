"""Retell completion task; preserves the Phase 4.5 registered task name.

Five retries with 0.25/0.5/1/2/3 second backoff (6.75 seconds total).
Two-second execution limits give an 18.75-second execution/backoff budget.
Queue latency is not bounded by Celery: expiry/deadline stops stale retries;
deployment must monitor queue latency to meet the end-to-end 20-second SLA.
"""
import logging
import time

from celery import shared_task
from billiard.exceptions import SoftTimeLimitExceeded
from django.db import OperationalError, InterfaceError, transaction
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_aware

from ..models import Interview, InterviewSession
from ..session_completion import finish_if_ready

logger = logging.getLogger(__name__)
_DELAYS = (0.25, 0.5, 1, 2, 3)


@shared_task(bind=True, name='interview_system.tasks.interviewing.process_retell_transcript',
             max_retries=5, soft_time_limit=1.5, time_limit=2, ignore_result=True)
def process_retell_transcript(self, payload: dict) -> dict:
    deadline = (self.request.headers or {}).get('retell_deadline', time.time() + 19)
    if time.time() >= deadline:
        logger.error('Retell transcript processing deadline exceeded.')
        return {'status': 'expired'}
    # Validate again at the worker boundary; malformed messages never retry.
    if not isinstance(payload, dict):
        return {'status': 'invalid'}
    call_id, transcript, ended = (payload.get(k) for k in ('retell_session_id', 'transcript', 'ended_at'))
    try:
        ended = parse_datetime(ended) if isinstance(ended, str) else None
    except ValueError:
        ended = None
    if (not isinstance(call_id, str) or not call_id.strip() or len(call_id) > 255
            or not isinstance(transcript, str) or ended is None or not is_aware(ended)):
        logger.warning('Invalid Retell transcript task payload dropped.')
        return {'status': 'invalid'}
    try:
        with transaction.atomic():
            matches = list(Interview.objects.select_for_update().filter(retell_session_id=call_id)[:2])
            if len(matches) != 1:
                logger.warning('Retell transcript dropped: matching_interviews=%s retell_session_id=%r', len(matches), call_id)
                return {'status': 'dropped'}
            interview = matches[0]
            session = InterviewSession.objects.select_for_update().filter(interview=interview).first()
            if session is None:
                logger.warning('Retell transcript dropped: no local session interview_id=%s', interview.pk)
                return {'status': 'dropped'}
            # Duplicate authenticated delivery is idempotent; do not overwrite the
            # accepted transcript or touch video's ended_at, including empty audio.
            if session.audio_ended_at is None:
                session.transcript = transcript
                session.audio_ended_at = ended
                session.save(update_fields=['transcript', 'audio_ended_at'])
            finish_if_ready(interview, session)
        return {'status': 'processed', 'session_id': str(session.pk)}
    except (OperationalError, InterfaceError, SoftTimeLimitExceeded):
        retry = self.request.retries
        if retry >= len(_DELAYS) or time.time() + _DELAYS[retry] >= deadline:
            logger.error('Retell transcript retry budget exhausted.')
            return {'status': 'exhausted'}
        # Leave atomic before retrying so the connection/transaction is usable.
        raise self.retry(countdown=_DELAYS[retry],
                         headers={'retell_deadline': deadline},
                         expires=max(0, deadline - time.time()),
                         argsrepr='(<redacted Retell transcript>,)') from None
