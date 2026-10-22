"""Verified media lifecycle and explicit versioned human decisions.

Existing interview mutations lock Interview -> Session (when needed) -> Application.
Decision writers lock existing Interviews in PK order before Application. Scheduling
locks Application and creates a new Interview; it never locks existing interviews.
"""
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Application, ApplicationDecision, Interview, ScreeningEvent
from .screening import ScreeningConflict


def stage_locked(interview, target, reason):
    app = Application.objects.select_for_update().get(pk=interview.application_id)
    latest = app.interviews.order_by('-created_at', '-pk').values_list('pk', flat=True).first()
    if latest != interview.pk:
        return  # Late evidence from a retained attempt cannot advance its replacement.
    if app.status not in (Application.Status.DECISION, Application.Status.REJECTED) and app.status != target:
        previous = app.status
        app.status, app.version = target, app.version + 1
        app.save(update_fields=["status", "version", "updated_at"])
        ScreeningEvent.objects.create(application=app, assessment_id=app.current_assessment_id,
            source="SYSTEM", from_status=previous, to_status=target, reason=reason)


def started_locked(interview):
    from .answer_evaluation import freeze_context_locked
    freeze_context_locked(interview)
    stage_locked(interview, Application.Status.IN_PROGRESS, "Verified provider session created or reconciled.")


def completed_locked(interview, session):
    """Call once under the interview lock when media is terminal, not on disconnect."""
    if interview.completed_at is not None:
        return
    failed = session.audio_terminal_status in ("error", "not_connected")
    usable = session.audio_terminal_status == "ended" and bool(session.transcript.strip())
    interview.completed_at = timezone.now()
    interview.completion_outcome = "FAILED" if failed else "USABLE" if usable else "INSUFFICIENT_EVIDENCE"
    interview.evaluation_status = "PENDING" if usable and not failed else "UNAVAILABLE"
    prep = getattr(interview, 'transcript_preparation', None)
    if prep and prep.state == 'WAITING' and not failed:
        interview.evaluation_status = 'PENDING'
    interview.save(update_fields=["completed_at", "completion_outcome", "evaluation_status", "updated_at"])
    stage_locked(interview, Application.Status.INTERVIEWED if usable and not failed else Application.Status.INTERVIEW_FAILED,
        "Verified interview completion: " + interview.completion_outcome)


def workflow_state(interview):
    if interview.status == Interview.Status.SCHEDULED:
        return "SCHEDULED"
    if interview.status == Interview.Status.LIVE:
        return "IN_PROGRESS"
    from .conversation_control import normal_completion_allowed
    if not normal_completion_allowed(interview):
        return 'INTERRUPTED'
    prep = getattr(interview, 'transcript_preparation', None)
    if prep and prep.state == 'WAITING':
        return 'TRANSCRIPT_PENDING'
    if interview.completion_outcome == "LEGACY_UNVERIFIED":
        return "LEGACY_UNVERIFIED"
    if interview.completion_outcome != "USABLE":
        return "FAILED" if interview.completion_outcome == "FAILED" else "INSUFFICIENT_EVIDENCE"
    return {"READY": "EVALUATED", "PENDING": "EVALUATION_PENDING", "FAILED": "EVALUATION_FAILED"}.get(
        interview.evaluation_status, "COMPLETED")


@transaction.atomic
def decide(application_id, *, actor, expected_version, outcome, reason, override=False, override_reason=""):
    interviews = list(Interview.objects.select_for_update().filter(application_id=application_id).order_by("pk"))
    app = Application.objects.select_for_update(of=("self",)).select_related("job__recruiter").get(pk=application_id)
    if Interview.objects.filter(application=app).exclude(pk__in=[row.pk for row in interviews]).exists():
        raise ScreeningConflict("An interview was scheduled concurrently. Refresh before deciding.")
    if actor.role != "RECRUITER" or app.job.recruiter.user_id != actor.pk:
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("Only the owning recruiter can decide this application.")
    if app.version != expected_version:
        raise ScreeningConflict()
    if app.status in (Application.Status.DECISION, Application.Status.REJECTED):
        raise ScreeningConflict("This application already has a terminal decision.")
    if outcome not in ("HIRE", "REJECT") or not reason.strip():
        raise ValidationError("An explicit outcome and decision reason are required.")
    latest = max(interviews, key=lambda row: (row.created_at, str(row.pk))) if interviews else None
    evaluation = latest.evaluations.order_by('-revision').first() if latest else None
    ready = (latest is not None and latest.status == "DONE" and latest.completed_at is not None
        and latest.completion_outcome == "USABLE" and latest.evaluation_status == "READY"
        and evaluation is not None and evaluation.status == 'READY' and evaluation.coverage == 1
        and evaluation.score is not None and evaluation.published_at is not None)
    from .conversation_control import normal_completion_allowed
    ready = ready and normal_completion_allowed(latest)
    if not ready and not override:
        raise ValidationError("A completed usable and evaluated interview is required; an early decision requires an explicit override.")
    if override and not override_reason.strip():
        raise ValidationError({"override_reason": "Explain why this decision proceeds without the normal eligibility checks."})
    previous = app.status
    app.status, app.version = Application.Status.DECISION, app.version + 1
    app.save(update_fields=["status", "version", "updated_at"])
    if not ApplicationDecision.objects.filter(application=app).exists():
        ApplicationDecision.objects.create(application=app, actor=actor, outcome=outcome, reason=reason.strip(),
            override=override, override_reason=override_reason.strip(), application_version=app.version, interview=latest, evaluation=evaluation)
    from .models import WorkflowAction
    WorkflowAction.objects.create(application=app, interview=latest, actor=actor, action='decision.recorded',
        expected_version=expected_version, reason=reason.strip(), details={
            'outcome': outcome, 'override': override, 'override_reason': override_reason.strip(),
            'evaluation': str(evaluation.pk) if evaluation else None, 'application_version': app.version})
    ScreeningEvent.objects.create(application=app, assessment_id=app.current_assessment_id, actor=actor,
        source="MANUAL", from_status=previous, to_status="DECISION", reason=reason.strip())
    from .audit import record_audit
    record_audit(actor=actor, action="application.decision_recorded", target=app,
        changed_fields=["status", "hiring_decision"], revision=app.version, application_id=app.pk,
        interview_id=latest.pk if latest else None)
    from .notifications import queue_notification
    queue_notification(app.candidate.user_id, "application_status_changed", resource_type='application',
        resource_id=app.pk, revision=app.version, links={'interview_id': latest.pk} if latest else {})
    return app
