"""Durable, repeatable application screening. Beat recovers unpublished/expired work."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import logging
import random
import uuid

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from ..models import Application, ApplicationAssessment, ParsedResume, Resume, ScreeningEvent
from ..screening import ACTIVE_STAGES, dispatch, evaluate

logger = logging.getLogger(__name__)
LEASE = timedelta(minutes=10)
MAX_ATTEMPTS = 4


class ParseBusy(Exception):
    pass


class ScreeningFailure(Exception):
    def __init__(self, code, message, action, transient=False):
        self.code, self.message, self.action, self.transient = code, message, action, transient


def parsed_resume(resume_id):
    from ..resumes.extraction import extract_text, ExtractionError
    from ..integrations.gemini_client import parse_resume_text, GeminiParseError
    from ai.matching.cache import get_cached_gemini_parse, set_cached_gemini_parse

    parser_version = "gemini-v1:" + settings.GEMINI_MODEL_NAME
    token = uuid.uuid4()
    with transaction.atomic():
        resume = Resume.objects.select_for_update().get(pk=resume_id)
        parsed = ParsedResume.objects.filter(resume=resume).first()
        if parsed and resume.status == "PARSED" and resume.parser_version == parser_version:
            return parsed, resume.content_hash, parser_version
        if resume.parse_lease_until and resume.parse_lease_until > timezone.now():
            raise ParseBusy()
        resume.parse_claim, resume.parse_lease_until = token, timezone.now() + LEASE
        resume.save(update_fields=["parse_claim", "parse_lease_until"])
    try:
        with resume.file.open("rb") as source:
            content_hash = hashlib.file_digest(source, "sha256").hexdigest()
        text = extract_text(resume.file).replace("\x00", "")
        if not text.strip():
            raise ExtractionError("No readable text.")
        if not settings.GEMINI_API_KEY:
            raise ScreeningFailure("PARSER_NOT_CONFIGURED", "Resume processing is temporarily unavailable. Contact support.", "CONTACT_SUPPORT")
        # Include parser identity in cache key so configuration changes cannot reuse stale parses.
        cache_key = parser_version + "\n" + text
        data = get_cached_gemini_parse(cache_key)
        if data is None:
            data = parse_resume_text(text)
            set_cached_gemini_parse(cache_key, data)
        with transaction.atomic():
            locked = Resume.objects.select_for_update().get(pk=resume_id)
            if locked.parse_claim != token:
                raise ParseBusy()
            parsed, _ = ParsedResume.objects.update_or_create(resume=locked, defaults={
                "raw_text": text, **{k: data[k] for k in ("skills", "education", "experience", "certifications")},
            })
            locked.status, locked.processing_error = "PARSED", ""
            locked.content_hash, locked.parser_version = content_hash, parser_version
            locked.parse_claim, locked.parse_lease_until = None, None
            locked.save()
        return parsed, content_hash, parser_version
    except (ExtractionError, FileNotFoundError):
        Resume.objects.filter(pk=resume_id, parse_claim=token).update(status="FAILED", processing_error="Upload a readable PDF or DOCX.")
        raise ScreeningFailure("UNREADABLE_RESUME", "Upload a readable PDF or DOCX, then retry screening.", "REPLACE_RESUME") from None
    except GeminiParseError:
        raise ScreeningFailure("PARSER_UNAVAILABLE", "Resume parsing failed. Retry screening after the service recovers.", "RETRY", True) from None
    finally:
        Resume.objects.filter(pk=resume_id, parse_claim=token).update(parse_claim=None, parse_lease_until=None)


@shared_task(ignore_result=True, acks_late=True, soft_time_limit=540, time_limit=570)
def process_assessment(assessment_id):
    token = uuid.uuid4()
    with transaction.atomic():
        assessment = ApplicationAssessment.objects.select_for_update().get(pk=assessment_id)
        if assessment.status not in ACTIVE_STAGES or assessment.next_attempt_at > timezone.now():
            return
        if assessment.lease_until and assessment.lease_until > timezone.now():
            return
        if not Application.objects.filter(pk=assessment.application_id, current_assessment=assessment).exists():
            return
        if assessment.attempts >= MAX_ATTEMPTS:
            assessment.status, assessment.error_code = "FAILED", "RECOVERY_EXHAUSTED"
            assessment.error_message, assessment.recovery_action = "Screening was interrupted repeatedly. Retry screening.", "RETRY"
            assessment.completed_at = timezone.now()
            assessment.save()
            return
        assessment.claim_token, assessment.lease_until = token, timezone.now() + LEASE
        assessment.status, assessment.attempts = "PARSING", assessment.attempts + 1
        assessment.save()
    claimed = ApplicationAssessment.objects.filter(pk=assessment_id, claim_token=token)
    try:
        parsed, content_hash, parser_version = parsed_resume(assessment.resume_id)
        if not claimed.update(status="MATCHING", resume_hash=content_hash, parser_version=parser_version):
            return
        from ai.matching.sbert import match
        policy = assessment.policy
        result = match(parsed.raw_text, "\n".join(policy[k] for k in ("title", "description", "requirements")), policy["skills_required"])
        score = Decimal(str(result["similarity"] * 100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if not score.is_finite() or not 0 <= score <= 100:
            raise ValueError("Invalid matcher score")
        if not claimed.update(status="SCREENING"):
            return
        criteria, eligibility, outcome, reasons = evaluate(policy, parsed, score)
        # Consistent order: application before assessment for both API mutations and task completion.
        with transaction.atomic():
            app = Application.objects.select_for_update().get(pk=assessment.application_id)
            current = ApplicationAssessment.objects.select_for_update().get(pk=assessment_id)
            if current.claim_token != token or app.current_assessment_id != current.pk:
                return
            current.match_score, current.criteria_results = score, criteria
            current.matched_skills = [r["requirement"] for r in criteria if r["kind"] == "skill" and r["status"] == "PASS"]
            current.missing_skills = [r["requirement"] for r in criteria if r["kind"] == "skill" and r["status"] != "PASS"]
            current.eligibility, current.outcome, current.reasons = eligibility, outcome, reasons
            current.status, current.completed_at, current.lease_until = "SUCCEEDED", timezone.now(), None
            current.claim_token = None
            current.error_code = current.error_message = current.recovery_action = ""
            current.save()
            if current.apply_decision and app.status in ("APPLIED", "UNDER_REVIEW"):
                old = app.status
                app.status, app.version = outcome, app.version + 1
                app.save(update_fields=["status", "version", "updated_at"])
                ScreeningEvent.objects.create(application=app, assessment=current, source="AUTOMATIC",
                                              from_status=old, to_status=outcome, reason=", ".join(reasons))
    except ParseBusy:
        claimed.update(status="QUEUED", attempts=F("attempts") - 1, claim_token=None, lease_until=None,
                       next_attempt_at=timezone.now() + timedelta(seconds=15))
    except Exception as exc:
        failure = exc if isinstance(exc, ScreeningFailure) else ScreeningFailure(
            "SCREENING_SERVICE_ERROR", "Screening could not finish. Retry screening.", "RETRY", True)
        retry = failure.transient and assessment.attempts < MAX_ATTEMPTS
        claimed.update(status="QUEUED" if retry else "FAILED", claim_token=None, lease_until=None,
                       error_code=failure.code, error_message=failure.message, recovery_action=failure.action,
                       next_attempt_at=timezone.now() + timedelta(seconds=min(300, 15 * 2 ** assessment.attempts) + random.randint(0, 10)),
                       completed_at=None if retry else timezone.now())
        logger.warning("Screening attempt failed: assessment_id=%s code=%s", assessment_id, failure.code)


@shared_task(ignore_result=True)
def recover_stalled_assessments():
    due = ApplicationAssessment.objects.filter(status__in=ACTIVE_STAGES, next_attempt_at__lte=timezone.now(),
        application__current_assessment_id=F("pk")).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=timezone.now()))
    for pk in due.order_by("next_attempt_at").values_list("pk", flat=True)[:100]:
        dispatch(pk)
