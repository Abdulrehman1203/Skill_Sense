"""
Interview Lifecycle Celery tasks (Phases 7 & 9 stubs).

Contains schedule_interview, generate_questions, process_retell_transcript.
Carries real retry policy (3x exponential backoff) and real DB persistence.
"""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)

RETRY_POLICY = {
    "bind": True,
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "retry_backoff_max": 600,
    "max_retries": 3,
}


@shared_task(**RETRY_POLICY)
def schedule_interview(self, application_id: str) -> dict:
    """
    Phase 9: Create Interview and InterviewSession records for an application.

    Contract:
      Input: application_id (UUID string)
      Output: dict with keys {interview_id, session_id, status}
      DB persistence: Creates Interview and InterviewSession records.
    """
    from ..models import Application, Interview, InterviewSession

    logger.info("schedule_interview task executing for application_id=%s (attempt %s)", application_id, self.request.retries + 1)

    try:
        app = Application.objects.get(pk=application_id)
    except Application.DoesNotExist:
        logger.error("Application %s not found", application_id)
        raise

    interview, created = Interview.objects.get_or_create(
        application=app,
        defaults={"status": Interview.Status.SCHEDULED},
    )

    session, _ = InterviewSession.objects.get_or_create(
        interview=interview,
    )

    result = {
        "interview_id": str(interview.id),
        "session_id": str(session.id),
        "status": interview.status,
    }

    logger.info("schedule_interview completed for application_id=%s, interview_id=%s", application_id, interview.id)

    return result


@shared_task(**RETRY_POLICY)
def generate_questions(self, interview_id: str) -> list[dict]:
    """
    Phase 7: Generate / select interview questions for an interview.

    The Gemini integration owns its three-attempt, sub-30-second retry budget.
    This task treats template fallback as a successful result and only relies
    on Celery retries for failures outside that external-call policy, such as
    temporary database errors.
    """
    from django.db import transaction

    from ..integrations import gemini_client
    from ..models import Interview, Question

    logger.info("generate_questions task executing for interview_id=%s (attempt %s)", interview_id, self.request.retries + 1)

    try:
        interview = Interview.objects.select_related(
            "application__job", "application__resume__parsed_data"
        ).get(pk=interview_id)
    except Interview.DoesNotExist:
        logger.error("Interview %s not found", interview_id)
        raise

    application = interview.application
    existing = list(interview.questions.values("id", "text", "category", "source", "approved"))
    if existing:
        return [{**item, "id": str(item["id"])} for item in existing]
    parsed_resume = getattr(application.resume, "parsed_data", None)
    try:
        questions_data = gemini_client.generate_questions_for(
            application.job, parsed_resume
        )
    except Exception as exc:
        # Defensive boundary: Step 1 already handles Gemini failures internally,
        # but the task must still never persist an empty interview if that
        # integration unexpectedly raises.
        logger.warning(
            "Question integration failed for interview %s; using templates: %s",
            interview_id,
            exc,
        )
        questions_data = gemini_client.get_template_questions()

    if not questions_data:
        questions_data = gemini_client.get_template_questions()

    question_rows = [
        Question(
            interview=interview,
            text=item["text"],
            category=item["category"],
            source=item["source"],
            approved=False,
        )
        for item in questions_data
    ]
    with transaction.atomic():
        # Duplicate delivery must preserve recruiter edits, approvals and IDs.
        Interview.objects.select_for_update().get(pk=interview.pk)
        existing = list(interview.questions.values("id", "text", "category", "source", "approved"))
        if existing:
            return [{**item, "id": str(item["id"])} for item in existing]
        created = Question.objects.bulk_create(question_rows)

    created_questions = [
        {
            "id": str(question.pk),
            "text": question.text,
            "category": question.category,
            "source": question.source,
            "approved": question.approved,
        }
        for question in created
    ]

    logger.info("generate_questions completed for interview_id=%s (%d questions created)", interview_id, len(created_questions))

    return created_questions


# Preserve old imports and the registered Celery name for existing producers.
from .interview_tasks import process_retell_transcript  # noqa: F401
