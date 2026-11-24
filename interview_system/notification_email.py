"""Render candidate emails from the resources associated with an outbox event."""

from django.template.loader import render_to_string

from .models import Application, Interview
from .notification_contract import EVENT_MESSAGES


def candidate_email(row):
    if row.event == "interview_scheduled":
        interview = Interview.objects.select_related("application__job__recruiter").filter(
            pk=row.resource_id, application__candidate_id=row.recipient_id,
        ).first()
        if interview:
            return "Your SkillSense interview is scheduled", render_candidate_email(
                "interview_scheduled", row, interview.application, interview=interview,
            )
    elif row.event == "application_status_changed":
        application = Application.objects.select_related("job__recruiter").filter(
            pk=row.resource_id, candidate_id=row.recipient_id,
        ).first()
        if application:
            outcome = decision_outcome(application, row.revision)
            if outcome == "HIRE":
                return "Congratulations on your SkillSense application", render_candidate_email(
                    "decision_hired", row, application,
                )
            if outcome == "REJECT":
                return "Decision on your SkillSense application", render_candidate_email(
                    "decision_rejected", row, application,
                )
    return EVENT_MESSAGES[row.event]


def decision_outcome(application, revision):
    if application.status == Application.Status.REJECTED and revision == str(application.version):
        return "REJECT"
    if application.status != Application.Status.DECISION:
        return None

    # A replacement decision can have a different outcome from the first decision.
    action = application.workflow_actions.filter(
        action="decision.recorded",
    ).order_by("-expected_version").first()
    if action:
        return action.details.get("outcome") if revision == str(action.expected_version + 1) else None

    decision = getattr(application, "hiring_decision", None)
    if decision and revision == str(decision.application_version):
        return decision.outcome
    return None


def render_candidate_email(template, row, application, **context):
    return render_to_string(f"emails/{template}.txt", {
        "first_name": row.recipient.first_name.strip(),
        "job_title": application.job.title,
        "company_name": application.job.recruiter.company_name.strip(),
        **context,
    }).strip()
