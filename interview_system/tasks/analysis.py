"""
Behavioral analysis Celery stub (Phase 8).

The Phase 6 scoring task is implemented in scoring_tasks.py and re-exported
here to preserve existing chain imports.
Carries real retry policy (3x exponential backoff) and real DB persistence.
"""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)

RETRY_POLICY = {
    "bind": True,
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "retry_backoff_max": 600,
    "max_retries": 3,
}


@shared_task(**RETRY_POLICY)
def aggregate_behavioral(self, session_id: str) -> dict:
    """
    Phase 8: Aggregate behavioral analysis for an interview session.

    Contract:
      Input: session_id (UUID string)
      Output: dict with keys {attention_pct, look_away_count, integrity_flags, emotion_summary}
      DB persistence: Creates/updates BehavioralAnalysis row for session_id.
    """
    from ..models import BehavioralAnalysis, InterviewSession

    logger.info("aggregate_behavioral task executing for session_id=%s (attempt %s)", session_id, self.request.retries + 1)

    try:
        session = InterviewSession.objects.get(pk=session_id)
    except InterviewSession.DoesNotExist:
        logger.error("InterviewSession %s not found", session_id)
        raise

    fake_behavioral = {
        "attention_pct": 94.5,
        "look_away_count": 2,
        "integrity_flags": [],
        "emotion_summary": {"confident": 0.8, "neutral": 0.2},
    }

    analysis_obj, _ = BehavioralAnalysis.objects.update_or_create(
        session=session,
        defaults={
            "attention_pct": fake_behavioral["attention_pct"],
            "look_away_count": fake_behavioral["look_away_count"],
            "integrity_flags": fake_behavioral["integrity_flags"],
            "emotion_summary": fake_behavioral["emotion_summary"],
        },
    )

    logger.info("aggregate_behavioral completed for session_id=%s, BehavioralAnalysis pk=%s", session_id, analysis_obj.pk)

    # Trigger next stage: candidate score generation
    application_id = str(session.interview.application_id)
    from .analysis import generate_candidate_score
    generate_candidate_score.delay(application_id=application_id)

    return fake_behavioral


from .scoring_tasks import generate_candidate_score
