"""
Parsing and Match Scoring Celery tasks (Phase 5).

Contains parse_resume and compute_match_score with:
- Text extraction (pdfplumber / python-docx / OCR fallback)
- Gemini API parsing with schema validation and Redis response caching
- SBERT cosine similarity matching with independent Redis embedding caching
- Exception handling (ExtractionError → non-retryable FAILED, GeminiParseError → autoretry)
- Failure-state persistence (Resume.status = FAILED on exhausted retries)
"""

from __future__ import annotations

import logging

from celery import shared_task

from ..integrations.gemini_client import GeminiParseError
from ..resumes.extraction import ExtractionError

logger = logging.getLogger(__name__)


def _mark_resume_failed(resume_id: str) -> None:
    """Mark a Resume row as FAILED — invoked on permanent failures or exhausted retries."""
    from ..models import Resume

    try:
        Resume.objects.filter(pk=resume_id).update(status=Resume.Status.FAILED)
        logger.error("Resume marked as FAILED: resume_id=%s", resume_id)
    except Exception as exc:
        logger.error("Could not set Resume status=FAILED for %s: %s", resume_id, exc)


# ═══════════════════════════════════════════════════════════════
#  parse_resume
# ═══════════════════════════════════════════════════════════════


@shared_task(
    bind=True,
    autoretry_for=(GeminiParseError,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
)
def parse_resume(self, resume_id: str) -> dict:
    """
    Parse a resume file and persist structured data into ParsedResume.

    Steps:
      1. Load Resume row
      2. extract_text(resume.file) → raw_text
      3. Check Gemini parse cache / call gemini_client.parse_resume_text(raw_text)
      4. Persist ParsedResume row & set Resume.status = PARSED
      5. Chain → compute_match_score.delay(resume_id)

    Failure semantics:
      - ExtractionError: Non-retryable (corrupt file). Immediately sets Resume.status = FAILED.
      - GeminiParseError: Retried up to 3× with exponential backoff.
        On exhaustion, sets Resume.status = FAILED and stops the chain.
    """
    from ..models import ParsedResume, Resume

    logger.info(
        "parse_resume executing for resume_id=%s (attempt %d/%d)",
        resume_id,
        self.request.retries + 1,
        self.max_retries + 1,
    )

    # 1. Load Resume
    try:
        resume = Resume.objects.get(pk=resume_id)
    except Resume.DoesNotExist:
        logger.error("Resume %s not found during parse_resume execution", resume_id)
        raise

    # 2. Extract text (non-retryable failure on corrupt / unsupported format)
    try:
        from ..resumes.extraction import extract_text

        raw_text = extract_text(resume.file)
    except ExtractionError as exc:
        logger.error("Text extraction failed permanently for resume %s: %s", resume_id, exc)
        _mark_resume_failed(resume_id)
        return {"error": str(exc), "status": "FAILED"}

    # 3. Parse via Gemini (wrapped behind cache check)
    from ai.matching.cache import get_cached_gemini_parse, set_cached_gemini_parse
    from ..integrations.gemini_client import parse_resume_text

    parsed_data = get_cached_gemini_parse(raw_text)
    if parsed_data is None:
        try:
            parsed_data = parse_resume_text(raw_text)
            set_cached_gemini_parse(raw_text, parsed_data)
        except GeminiParseError:
            if self.request.retries >= self.max_retries:
                _mark_resume_failed(resume_id)
            raise

    # 4. Persist ParsedResume row
    parsed_obj, _ = ParsedResume.objects.update_or_create(
        resume=resume,
        defaults={
            "skills": parsed_data["skills"],
            "education": parsed_data["education"],
            "experience": parsed_data["experience"],
            "certifications": parsed_data["certifications"],
            "raw_text": raw_text,
        },
    )

    resume.status = Resume.Status.PARSED
    resume.save(update_fields=["status"])

    logger.info(
        "parse_resume completed for resume_id=%s, ParsedResume pk=%s",
        resume_id,
        parsed_obj.pk,
    )

    # 5. Chain → compute_match_score
    compute_match_score.delay(resume_id=resume_id)

    return {
        "skills": parsed_data["skills"],
        "education": parsed_data["education"],
        "experience": parsed_data["experience"],
        "certifications": parsed_data["certifications"],
        "raw_text": raw_text[:200],
    }


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
    """
    Compute job-relative match score and skills breakdown via SBERT.

    Steps:
      1. Load ParsedResume + linked Job
      2. ai.matching.sbert.match(raw_text, job_text, job_skills)
      3. Write match_score, matched_skills, missing_skills to ParsedResume
      4. Chain → generate_candidate_score.delay(application_id)
    """
    from ..models import Application, ParsedResume

    logger.info(
        "compute_match_score executing for resume_id=%s (attempt %d)",
        resume_id,
        self.request.retries + 1,
    )

    try:
        parsed_resume = ParsedResume.objects.select_related("resume").get(
            resume_id=resume_id
        )
    except ParsedResume.DoesNotExist:
        logger.error("ParsedResume for resume_id=%s not found", resume_id)
        _mark_resume_failed(resume_id)
        raise

    app = Application.objects.select_related("job").filter(resume_id=resume_id).first()
    if not app:
        logger.error("No Application found for resume_id=%s", resume_id)
        _mark_resume_failed(resume_id)
        return {"error": "No linked application found", "status": "FAILED"}

    job = app.job
    job_text = f"{job.title}\n{job.description}\n{job.requirements}"
    job_skills = list(job.skills_required) if job.skills_required else []

    # SBERT matching
    from ai.matching.sbert import match as sbert_match

    match_result = sbert_match(
        resume_text=parsed_resume.raw_text,
        job_text=job_text,
        job_skills=job_skills,
    )

    # Persist match results (score on 0-100 scale)
    parsed_resume.match_score = round(match_result["similarity"] * 100, 2)
    parsed_resume.matched_skills = match_result["matched_skills"]
    parsed_resume.missing_skills = match_result["missing_skills"]
    parsed_resume.save(
        update_fields=["match_score", "matched_skills", "missing_skills"]
    )

    logger.info(
        "compute_match_score completed for resume_id=%s, score=%.2f",
        resume_id,
        parsed_resume.match_score,
    )

    # Chain → Phase 6 stub
    from .analysis import generate_candidate_score

    generate_candidate_score.delay(application_id=str(app.id))

    return {
        "match_score": parsed_resume.match_score,
        "matched_skills": match_result["matched_skills"],
        "missing_skills": match_result["missing_skills"],
    }
