"""
Analysis & Candidate Scoring Celery tasks (Phases 6 & 8 stubs).

Contains aggregate_behavioral and generate_candidate_score.
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


@shared_task(**RETRY_POLICY)
def generate_candidate_score(self, application_id: str) -> dict:
    """
    Phase 6: Compute aggregate candidate score for an application.

    Contract:
      Input: application_id (UUID string)
      Output: dict with keys {final_score, breakdown, explanation}
      DB persistence: Creates/updates CandidateScore row for application_id.
    """
    from ..models import Application, CandidateScore

    logger.info("generate_candidate_score task executing for application_id=%s (attempt %s)", application_id, self.request.retries + 1)

    try:
        app = Application.objects.get(pk=application_id)
    except Application.DoesNotExist:
        logger.error("Application %s not found", application_id)
        raise

    fake_score_data = {
        "final_score": 88.5,
        "breakdown": {
            "match_score": 85.0,
            "interview_score": 90.0,
            "behavioral_score": 92.0,
        },
        "explanation": "stub",
    }

    score_obj, _ = CandidateScore.objects.update_or_create(
        application=app,
        defaults={
            "final_score": fake_score_data["final_score"],
            "breakdown": fake_score_data["breakdown"],
            "explanation": fake_score_data["explanation"],
        },
    )

    logger.info("generate_candidate_score completed for application_id=%s, score=%s", application_id, score_obj.final_score)

    # Trigger notification to candidate
    candidate_user_id = str(app.candidate.user_id)
    from .notifications import send_notification
    send_notification.delay(
        user_id=candidate_user_id,
        event={
            "type": "APPLICATION_SCORED",
            "application_id": application_id,
            "final_score": fake_score_data["final_score"],
        },
    )

    return fake_score_data
