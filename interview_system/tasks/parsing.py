"""Compatibility tasks for resume-library parsing and application assessment dispatch."""

from __future__ import annotations

import logging

from celery import shared_task


logger = logging.getLogger(__name__)


def _mark_resume_failed(resume_id: str, reason: str) -> None:
    """Mark a Resume row as FAILED — invoked on permanent failures or exhausted retries."""
    from ..models import Resume

    try:
        Resume.objects.filter(pk=resume_id).update(
            status=Resume.Status.FAILED, processing_error=reason[:500]
        )
        logger.error("Resume marked as FAILED: resume_id=%s", resume_id)
    except Exception as exc:
        logger.error("Could not set Resume status=FAILED for %s: %s", resume_id, exc)


# ═══════════════════════════════════════════════════════════════
#  compute_match_score
# ═══════════════════════════════════════════════════════════════


@shared_task(
    bind=True,
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=600,
)
def compute_match_score(self, resume_id: str) -> dict:
    """Compatibility producer: fan out to application-owned assessments."""
    from ..models import Application
    from ..screening import create_assessment, dispatch
    count = 0
    for app in Application.objects.filter(resume_id=resume_id):
        assessment = app.current_assessment
        if assessment is None:
            assessment = create_assessment(app.pk, apply_decision=app.status in ("APPLIED", "UNDER_REVIEW"))
        else:
            dispatch(assessment.pk)
        count += 1
    return {"applications": count}


# ═══════════════════════════════════════════════════════════════
#  parse_resume
# ═══════════════════════════════════════════════════════════════


@shared_task(bind=True, max_retries=3, ignore_result=True)
def parse_resume(self, resume_id: str) -> dict:
    """Resume-library compatibility task using the same parse lease as assessments."""
    from .screening import parsed_resume, ParseBusy, ScreeningFailure
    try:
        parsed, _, _ = parsed_resume(resume_id)
    except ParseBusy:
        raise self.retry(countdown=15, max_retries=40)
    except ScreeningFailure as exc:
        if exc.transient and self.request.retries < 3:
            raise self.retry(countdown=min(300, 15 * 2 ** self.request.retries)) from None
        _mark_resume_failed(resume_id, exc.message)
        return {"status": "FAILED", "error": exc.code}
    except Exception:
        if self.request.retries < 3:
            raise self.retry(countdown=30) from None
        _mark_resume_failed(resume_id, "Resume processing failed. Retry or upload a readable document.")
        return {"status": "FAILED", "error": "PARSER_UNAVAILABLE"}
    compute_match_score.run(resume_id=resume_id)
    return {"status": "PARSED", "skills": parsed.skills, "education": parsed.education,
            "experience": parsed.experience, "certifications": parsed.certifications}
