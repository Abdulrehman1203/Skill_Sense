"""Phase 6 candidate scoring task.

Scoring writes a CandidateScore only. Application status is controlled solely
by the recruiter-driven application workflow.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db import InterfaceError, OperationalError

from ai.scoring.rubric import score

from ..models import Application, CandidateScore, ParsedResume, Resume, ScoringRubric

logger = logging.getLogger(__name__)


class ScoringConfigurationError(RuntimeError):
    """Scoring cannot run without exactly one active rubric."""


class ScoringInputNotReady(RuntimeError):
    """The Phase 5 match is not complete or failed."""


def _interview_signal(application_id: str) -> float | None:
    """Read a future Phase 9 session metric when that field exists."""
    try:
        session_model = apps.get_model("interview_system", "InterviewSession")
        session_model._meta.get_field("interview_signal")
    except (LookupError, FieldDoesNotExist):
        # Today's session/transcript stubs do not measure completeness.
        return None
    return (
        session_model.objects
        .filter(interview__application_id=application_id, interview_signal__isnull=False)
        .order_by("-interview__created_at", "-pk")
        .values_list("interview_signal", flat=True)
        .first()
    )


def _behavioral_signal(application_id: str) -> dict | None:
    """Read Phase 8 aggregate data if a linked analysis is available."""
    try:
        analysis_model = apps.get_model("interview_system", "BehavioralAnalysis")
    except LookupError:
        return None
    analysis = (
        analysis_model.objects
        .filter(session__interview__application_id=application_id, attention_pct__isnull=False)
        .order_by("-created_at", "-pk")
        .first()
    )
    if analysis is None:
        return None
    return {
        "attention_pct": analysis.attention_pct,
        "integrity_flag_count": len(analysis.integrity_flags or []),
    }


@shared_task(
    bind=True,
    name="interview_system.tasks.analysis.generate_candidate_score",
    autoretry_for=(OperationalError, InterfaceError),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
)
def generate_candidate_score(self, application_id: str) -> dict:
    """Score an application and persist the result without changing its status."""
    app = Application.objects.select_related("resume", "candidate").get(pk=application_id)
    parsed = ParsedResume.objects.filter(resume_id=app.resume_id).first()
    # A failed resume must never reuse a score left by an earlier attempt.
    match_score = (
        None if app.resume.status == Resume.Status.FAILED
        else parsed.match_score if parsed is not None else None
    )
    if match_score is None and app.resume.status != Resume.Status.FAILED:
        raise ScoringInputNotReady(
            f"Phase 5 match is not ready for application {application_id}."
        )

    try:
        rubric = ScoringRubric.objects.get(active=True)
    except (ScoringRubric.DoesNotExist, ScoringRubric.MultipleObjectsReturned) as exc:
        raise ScoringConfigurationError(
            "Exactly one active scoring rubric is required."
        ) from exc

    result = score(
        match_score=match_score,
        interview_signal=_interview_signal(application_id),
        behavioral_signal=_behavioral_signal(application_id),
        rubric=rubric,
    )
    CandidateScore.objects.update_or_create(
        application=app,
        defaults={
            "final_score": result["final_score"],
            "breakdown": result["breakdown"],
            "explanation": result["explanation"],
        },
    )
    logger.info("Candidate score persisted for application_id=%s", application_id)

    # The Phase 4.5 notification stub takes a User id and an event dictionary.
    # CandidateProfile uses User as its primary key, so candidate_id is that id.
    from .notifications import send_notification

    send_notification.delay(str(app.candidate_id), event={"type": "score_ready"})
    return result
