"""Application screening policy and state transitions, shared by HTTP and workers."""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError
from .models import Application, ApplicationAssessment, Job, ScreeningEvent

logger = logging.getLogger(__name__)
ACTIVE_STAGES = ("QUEUED", "PARSING", "MATCHING", "SCREENING")
POLICY_FIELDS = ("title", "description", "requirements", "skills_required", "screening_threshold",
                 "auto_shortlist_enabled", "non_pass_policy", "screening_criteria")


class ScreeningConflict(APIException):
    status_code = 409
    default_detail = "Application changed. Refresh before trying again."


def snapshot(job):
    return {**{name: getattr(job, name) for name in POLICY_FIELDS},
            "screening_threshold": str(job.screening_threshold),
            "version": job.screening_policy_version}


def dispatch(assessment_id):
    from .tasks.screening import process_assessment
    try:
        process_assessment.apply_async(args=[str(assessment_id)], retry=False)
    except Exception:
        # The persisted QUEUED stage is the durable dispatch record, recovered by Beat.
        logger.warning("Screening dispatch deferred: assessment_id=%s", assessment_id)


@transaction.atomic
def create_assessment(application_id, *, apply_decision=True, use_current_policy=True):
    app = Application.objects.select_for_update().get(pk=application_id)
    current = app.current_assessment
    if current and current.status in ACTIVE_STAGES:
        return current
    job = Job.objects.select_for_update().get(pk=app.job_id)
    revision = (app.assessments.aggregate(n=Max("revision"))["n"] or 0) + 1
    assessment = ApplicationAssessment.objects.create(
        application=app, resume_id=app.resume_id, revision=revision,
        policy=snapshot(job) if use_current_policy or not current else current.policy,
        apply_decision=apply_decision,
        matcher_version=getattr(settings, "SBERT_MODEL_NAME", "all-MiniLM-L6-v2"),
    )
    app.current_assessment = assessment
    app.version += 1
    app.save(update_fields=["current_assessment", "version", "updated_at"])
    transaction.on_commit(lambda: dispatch(assessment.pk))
    return assessment


def normalize(value):
    value = re.sub(r"\s+", " ", str(value).strip().lower())
    return {"py": "python", "js": "javascript", "postgres": "postgresql",
            "nodejs": "node.js", "node js": "node.js", "reactjs": "react",
            "c sharp": "c#", "csharp": "c#"}.get(value, value)


def criterion(kind, name, status, evidence=""):
    return {"kind": kind, "requirement": name, "status": status, "evidence": evidence}


def evaluate(policy, parsed, score):
    """Conservative evidence rules; omission is UNKNOWN, never an invented failure."""
    results = []
    skills = {normalize(s) for s in parsed.skills}
    required = policy.get("skills_required", [])
    for skill in required:
        found = normalize(skill) in skills
        results.append(criterion("skill", skill, "PASS" if found else "UNKNOWN",
                                 skill if found else "Not evidenced in extracted resume skills."))
    criteria = policy.get("screening_criteria", {})
    for cert in criteria.get("certifications", []):
        found = normalize(cert) in {normalize(c) for c in parsed.certifications}
        results.append(criterion("certification", cert, "PASS" if found else "UNKNOWN",
                                 cert if found else "Certification not evidenced."))
    education = criteria.get("education_levels", [])
    if education:
        degrees = [str(e.get("degree", "")) for e in parsed.education if isinstance(e, dict)]
        # Explicit equivalences only; no assumptions about institutions or incomplete study.
        def level(text):
            text = normalize(text)
            for key, aliases in {"doctorate": ("phd", "ph.d", "doctor"),
                                 "master": ("master", "msc", "m.sc", "ms "),
                                 "bachelor": ("bachelor", "bsc", "b.sc", "bs ")}.items():
                if any(a in text for a in aliases):
                    return key
            return text
        found = any(level(d) in {level(e) for e in education} for d in degrees)
        results.append(criterion("education", ", ".join(education), "PASS" if found else "UNKNOWN", "; ".join(degrees)))
    minimum = criteria.get("minimum_experience_years")
    if minimum is not None and Decimal(str(minimum)) > 0:
        # Count only explicitly dated, relevant employment. Ambiguous durations need review.
        intervals, ambiguous = [], False
        for entry in parsed.experience:
            if not isinstance(entry, dict):
                ambiguous = True
                continue
            evidence = normalize(entry.get("title", "") + " " + entry.get("description", ""))
            relevant = not required or any(re.search(r"(?<!\w)" + re.escape(normalize(s)) + r"(?!\w)", evidence) for s in required)
            dates = re.findall(r"\b((?:19|20)\d{2})[-/](0[1-9]|1[0-2])\b", str(entry.get("duration", "")))
            if not relevant or len(dates) != 2:
                ambiguous = True
                continue
            start, end = [int(y) * 12 + int(m) for y, m in dates]
            if end < start:
                ambiguous = True
                continue
            intervals.append((start, end))
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        years = Decimal(sum(b - a for a, b in merged)) / 12
        state = "PASS" if years >= Decimal(str(minimum)) else "UNKNOWN" if ambiguous or not intervals else "FAIL"
        results.append(criterion("experience", f"{minimum} relevant years", state,
                                 f"{years:.2f} years in explicit non-overlapping relevant date ranges."))
    if criteria.get("manual_review_required"):
        results.append(criterion("manual", "Additional mandatory requirements", "UNKNOWN", "Recruiter review required."))
    states = {r["status"] for r in results}
    eligibility = "UNKNOWN" if "UNKNOWN" in states else "FAIL" if "FAIL" in states else "PASS"
    reasons = []
    if eligibility == "UNKNOWN":
        reasons.append("REQUIREMENT_UNVERIFIED")
    if "FAIL" in states:
        reasons.append("REQUIREMENT_NOT_MET")
    if Decimal(str(score)) < Decimal(policy["screening_threshold"]):
        reasons.append("BELOW_THRESHOLD")
    if not policy["auto_shortlist_enabled"]:
        reasons.append("AUTOMATION_DISABLED")
        outcome = "UNDER_REVIEW"
    elif eligibility == "UNKNOWN":
        outcome = "UNDER_REVIEW"
    elif not reasons:
        outcome, reasons = "SHORTLISTED", ["QUALIFIED"]
    else:
        outcome = "REJECTED" if policy["non_pass_policy"] == "REJECT" else "UNDER_REVIEW"
    return results, eligibility, outcome, reasons


def permitted_actions(app, user):
    assessment = app.current_assessment
    if app.status not in ("APPLIED", "UNDER_REVIEW"):
        return []
    recruiter = getattr(user, "role", None) == "RECRUITER"
    if assessment and assessment.status == "FAILED":
        actions = ["retry-screening"] if assessment.recovery_action != "CONTACT_SUPPORT" or recruiter else []
        if recruiter and assessment.recovery_action == "REPLACE_RESUME":
            actions = []
        return actions + (["reject"] if recruiter else [])
    if assessment and assessment.status == "SUCCEEDED" and recruiter:
        return ["shortlist", "reject"] + (["rescreen"] if app.status == "UNDER_REVIEW" else [])
    return ["reject"] if recruiter else []


@transaction.atomic
def transition(application_id, target, *, actor, reason="", expected_version=None):
    app = Application.objects.select_for_update(of=("self",)).select_related("current_assessment").get(pk=application_id)
    if expected_version is None or expected_version != app.version:
        raise ScreeningConflict()
    if app.status not in ("APPLIED", "UNDER_REVIEW"):
        raise ScreeningConflict("This application can no longer be screened.")
    assessment = app.current_assessment
    if target == "SHORTLISTED":
        if not assessment or assessment.status != "SUCCEEDED":
            raise ValidationError("Complete screening before shortlisting.")
        if assessment.reasons != ["QUALIFIED"] and not reason.strip():
            raise ValidationError({"reason": "Explain the screening override."})
    elif target == "REJECTED":
        if not reason.strip():
            raise ValidationError({"reason": "A rejection reason is required."})
    else:
        raise ValidationError("Unsupported screening transition.")
    old = app.status
    app.status, app.version = target, app.version + 1
    app.save(update_fields=["status", "version", "updated_at"])
    ScreeningEvent.objects.create(application=app, assessment=assessment, actor=actor,
                                  source="MANUAL", from_status=old, to_status=target, reason=reason)
    return app
