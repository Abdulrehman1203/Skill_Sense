"""Question preparation, revisioned recruiter writes, and frozen call copies."""
import hashlib
import json
from types import SimpleNamespace

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from .models import PreparedQuestion, Question, QuestionSet


class QuestionConflict(APIException):
    status_code = 409
    default_detail = "Question set changed or is frozen. Refresh before retrying."


def snapshot_hash(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def prepare_locked(app, assessment):
    """Caller holds Application lock; persist work before publishing to Celery."""
    if (app.status != "SHORTLISTED" or app.current_assessment_id != assessment.pk
            or app.resume_id != assessment.resume_id or assessment.status != "SUCCEEDED"):
        raise QuestionConflict("Shortlist a successfully assessed application before preparation.")
    parsed = getattr(assessment.resume, "parsed_data", None)
    snapshot = {
        "assessment_id": str(assessment.pk), "assessment_revision": assessment.revision,
        "resume_hash": assessment.resume_hash, "policy": assessment.policy,
        "candidate": {k: getattr(parsed, k, []) for k in ("skills", "experience", "education", "certifications")},
    }
    question_set, _ = QuestionSet.objects.get_or_create(
        assessment=assessment,
        defaults={"application": app, "assessment_revision": assessment.revision,
                  "input_snapshot": snapshot, "input_hash": snapshot_hash(snapshot)},
    )
    transaction.on_commit(lambda: dispatch(question_set.pk))
    return question_set


def dispatch(set_id):
    from .tasks.questions import generate_question_set
    try:
        generate_question_set.apply_async(args=[str(set_id)], retry=False)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Question preparation dispatch deferred set_id=%s", set_id)


def generation_context(question_set):
    snapshot = question_set.input_snapshot
    if snapshot_hash(snapshot) != question_set.input_hash:
        raise ValueError("Question input snapshot hash mismatch")
    return SimpleNamespace(**snapshot["policy"]), SimpleNamespace(**snapshot["candidate"])


def edit_locked(question_set, data, actor):
    """Caller holds set lock. All commands are atomic and revision checked."""
    if question_set.frozen_at or data["expected_revision"] != question_set.revision:
        raise QuestionConflict()
    if question_set.generation_status not in ("READY", "FALLBACK", "LEGACY"):
        raise QuestionConflict("Question generation must finish before review.")
    rows = list(question_set.items.filter(archived_at__isnull=True))
    by_id = {str(q.pk): q for q in rows}
    operation = data["operation"]
    if operation == "approve":
        if not rows:
            raise ValidationError("Approve at least one question.")
        question_set.approved_at = timezone.now()
        question_set.approved_by = actor
    else:
        if operation == "add":
            if len(rows) >= 100:
                raise ValidationError("A question set may contain at most 100 active questions.")
            PreparedQuestion.objects.create(question_set=question_set, text=data["text"],
                category=data["category"], source="MANUAL", position=len(rows), generation_number=question_set.generation_number)
        elif operation in ("edit", "remove"):
            row = by_id.get(str(data["id"]))
            if row is None:
                raise ValidationError("Question ID must be active and belong to this set.")
            if operation == "remove":
                row.archived_at = timezone.now()
            else:
                row.recruiter_edited = True
                for field in ("text", "category"):
                    if field in data:
                        setattr(row, field, data[field])
            row.save()
        elif operation == "reorder":
            ids = [str(pk) for pk in data["ids"]]
            if len(ids) != len(set(ids)) or set(ids) != set(by_id):
                raise ValidationError("Supply every active question ID exactly once.")
            rows = [by_id[pk] for pk in ids]
        # Reassign consecutive positions after removal, too.
        rows = rows if operation == "reorder" else list(question_set.items.filter(archived_at__isnull=True))
        for position, row in enumerate(rows):
            row.position = position
        PreparedQuestion.objects.bulk_update(rows, ["position"])
        question_set.approved_at = None
        question_set.approved_by = None
    question_set.revision += 1
    question_set.save(update_fields=["revision", "approved_at", "approved_by"])
    from .audit import record_audit
    record_audit(actor=actor, action='questions.' + operation, target=question_set,
        changed_fields=['approval'] if operation == 'approve' else ['questions', 'approval'],
        revision=question_set.revision, application_id=question_set.application_id)


def freeze_locked(app, set_id, expected_revision):
    """Application -> QuestionSet lock order. Freeze at scheduling, before call."""
    question_set = QuestionSet.objects.select_for_update().filter(pk=set_id, application=app).first()
    if question_set is None:
        raise ValidationError({"question_set": "Select a question set belonging to this application."})
    if question_set.assessment_id != app.current_assessment_id or question_set.legacy:
        raise QuestionConflict("Select the current assessment's prepared question set.")
    if question_set.frozen_at or question_set.revision != expected_revision:
        raise QuestionConflict()
    if not question_set.approved_at or question_set.generation_status not in ("READY", "FALLBACK"):
        raise ValidationError({"question_set": "Recruiter approval is required before scheduling."})
    rows = list(question_set.items.filter(archived_at__isnull=True))
    if not rows:
        raise ValidationError({"question_set": "At least one approved question is required."})
    question_set.frozen_at = timezone.now()
    question_set.frozen_revision = question_set.revision
    question_set.save(update_fields=["frozen_at", "frozen_revision"])
    return question_set, rows


def bind_copies(interview, rows):
    Question.objects.bulk_create([Question(interview=interview, prepared_question=q,
        text=q.text, category=q.category, source=q.source, position=q.position, approved=True) for q in rows])
