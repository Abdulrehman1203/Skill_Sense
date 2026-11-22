"""Resolve the in-app context for a page of notifications."""
from collections import defaultdict
from uuid import UUID

from django.db.models import F

from .models import Application, FeedbackReport, Interview, InterviewEvaluation, QuestionSet, ScoreRevision, User
from .notification_contract import CANDIDATE_EVENTS, EVENT_RESOURCES


# Each resource is joined back to the application that grants access.
RESOURCES = {
    "application": (Application, ""),
    "interview": (Interview, "application"),
    "question_set": (QuestionSet, "application"),
    "evaluation": (InterviewEvaluation, "interview__application"),
    "score_revision": (ScoreRevision, "application"),
    "feedback_report": (FeedbackReport, "evaluation__interview__application"),
}


def link_id(links, resource):
    value = links.get(f"{resource}_id") if isinstance(links, dict) else None
    try:
        return UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def notification_details(notifications, user):
    if user is None or user.role not in (User.Role.CANDIDATE, User.Role.RECRUITER):
        return {}
    candidate = user.role == User.Role.CANDIDATE
    grouped = defaultdict(list)
    for notification in notifications:
        resource = EVENT_RESOURCES.get(notification.type)
        if (not resource or notification.user_id != user.pk
                or candidate != (notification.type in CANDIDATE_EVENTS)):
            continue
        resource_id = link_id(notification.links, resource)
        if resource_id is None:
            resource = "application"
            resource_id = link_id(notification.links, resource)
        if resource_id is not None:
            grouped[resource].append((notification, resource_id))

    details = {}
    for resource, rows in grouped.items():
        model, application_path = RESOURCES[resource]
        prefix = f"{application_path}__" if application_path else ""
        owner = "candidate_id" if candidate else "job__recruiter_id"
        fields = {
            "application_pk": F(f"{prefix}pk"),
            "job_title": F(f"{prefix}job__title"),
        }
        if candidate:
            fields["company_name"] = F(f"{prefix}job__recruiter__company_name")
            fields["application_status"] = F(f"{prefix}status")
            if resource == "interview":
                fields["interview_scheduled_at"] = F("scheduled_at")
                fields["interview_deadline_at"] = F("deadline_at")
        else:
            fields["first_name"] = F(f"{prefix}candidate__user__first_name")
            fields["last_name"] = F(f"{prefix}candidate__user__last_name")
        records = model.objects.filter(
            pk__in={resource_id for _, resource_id in rows},
            **{f"{prefix}{owner}": user.pk},
        ).order_by().values("pk", **fields)
        resources = {record["pk"]: record for record in records}

        for notification, resource_id in rows:
            record = resources.get(resource_id)
            if record is None:
                continue
            linked_application = link_id(notification.links, "application")
            if linked_application is not None and linked_application != record["application_pk"]:
                continue

            payload = {"job_title": record["job_title"].strip() or None}
            if candidate:
                payload["company_name"] = record["company_name"].strip() or None
                if notification.type == "application_status_changed":
                    payload["current_application_status"] = dict(Application.Status.choices).get(
                        record["application_status"],
                    )
                if resource == "interview":
                    payload["scheduled_at"] = record["interview_scheduled_at"]
                    payload["deadline_at"] = record["interview_deadline_at"]
            else:
                payload["candidate_name"] = " ".join(
                    name for name in (record["first_name"].strip(), record["last_name"].strip()) if name
                ) or None
            details[notification.pk] = {key: value for key, value in payload.items() if value is not None}
    return details
