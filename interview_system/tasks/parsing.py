"""Compatibility tasks for resume-library parsing and application assessment dispatch."""

from __future__ import annotations

import logging
import random

from celery import shared_task
from celery.exceptions import Reject


logger = logging.getLogger(__name__)


def _mark_resume_failed(resume_id: str, reason: str, *, retry_available: bool) -> None:
    """Mark a Resume row as FAILED — invoked on permanent failures or exhausted retries."""
    from ..models import Resume

    try:
        updated = Resume.objects.filter(pk=resume_id, status=Resume.Status.PENDING, parse_claim__isnull=True).update(
            status=Resume.Status.FAILED, processing_error=reason[:500],
            processing_code=Resume.ProcessingCode.RETRY_AVAILABLE if retry_available else Resume.ProcessingCode.SUPPORT_REQUIRED,
        )
        if updated:
            logger.error("Resume marked as FAILED: resume_id=%s", resume_id)
    except Exception:
        logger.error("Could not set Resume status=FAILED for resume_id=%s", resume_id)


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
            try:
                raise self.retry(countdown=15 * 2 ** self.request.retries + random.randint(0, 5)) from None
            except Reject:
                _mark_resume_failed(resume_id, exc.message, retry_available=True)
                raise
        _mark_resume_failed(resume_id, exc.message, retry_available=exc.transient)
        return {"status": "FAILED", "error": exc.code}
    except Exception:
        if self.request.retries < 3:
            raise self.retry(countdown=30) from None
        _mark_resume_failed(resume_id, "Resume processing failed. Retry parsing on the existing upload.", retry_available=True)
        return {"status": "FAILED", "error": "PARSER_UNAVAILABLE"}
    compute_match_score.run(resume_id=resume_id)
    return {"status": "PARSED", "skills": parsed.skills, "education": parsed.education,
            "experience": parsed.experience, "certifications": parsed.certifications}
