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

    # Trigger next stage in pipeline: question generation
    from .interviewing import generate_questions
    generate_questions.delay(interview_id=str(interview.id))

    return result


@shared_task(**RETRY_POLICY)
def generate_questions(self, interview_id: str) -> list[dict]:
    """
    Phase 7: Generate / select interview questions for an interview.

    Contract:
      Input: interview_id (UUID string)
      Output: list of 3 question dicts {text, category, source}
      DB persistence: Creates 3 Question rows in DB for interview_id.
    """
    from ..models import Interview, Question

    logger.info("generate_questions task executing for interview_id=%s (attempt %s)", interview_id, self.request.retries + 1)

    try:
        interview = Interview.objects.get(pk=interview_id)
    except Interview.DoesNotExist:
        logger.error("Interview %s not found", interview_id)
        raise

    questions_data = [
        {
            "text": "Explain Django's ORM query evaluation and prefetch_related.",
            "category": Question.Category.TECHNICAL,
            "source": Question.Source.TEMPLATE,
        },
        {
            "text": "Describe a situation where you resolved a team technical disagreement.",
            "category": Question.Category.BEHAVIORAL,
            "source": Question.Source.TEMPLATE,
        },
        {
            "text": "How would you handle a sudden traffic spike causing high latency?",
            "category": Question.Category.SITUATIONAL,
            "source": Question.Source.TEMPLATE,
        },
    ]

    # Clean existing questions for stub idempotency
    Question.objects.filter(interview=interview).delete()

    created_questions = []
    for q_data in questions_data:
        q = Question.objects.create(
            interview=interview,
            text=q_data["text"],
            category=q_data["category"],
            source=q_data["source"],
            approved=True,
        )
        created_questions.append({
            "id": str(q.id),
            "text": q.text,
            "category": q.category,
            "source": q.source,
        })

    logger.info("generate_questions completed for interview_id=%s (%d questions created)", interview_id, len(created_questions))

    # Trigger next stage in pipeline: retell transcript processing stub
    from .interviewing import process_retell_transcript
    process_retell_transcript.delay(payload={
        "interview_id": interview_id,
        "transcript": "Candidate answered all 3 technical and behavioral questions clearly with strong problem-solving examples.",
    })

    return created_questions


@shared_task(**RETRY_POLICY)
def process_retell_transcript(self, payload: dict) -> dict:
    """
    Phase 9: Process Retell voice interview webhook transcript.

    Contract:
      Input: payload dict containing {interview_id / session_id, transcript}
      Output: dict with keys {session_id, transcript}
      DB persistence: Writes transcript verbatim to InterviewSession and sets Interview.status = DONE.
    """
    from ..models import Interview, InterviewSession

    logger.info("process_retell_transcript executing for payload=%s (attempt %s)", payload, self.request.retries + 1)

    interview_id = payload.get("interview_id")
    session_id = payload.get("session_id")
    transcript_text = payload.get("transcript", "Stub transcript text.")

    if session_id:
        try:
            session = InterviewSession.objects.get(pk=session_id)
            interview = session.interview
        except InterviewSession.DoesNotExist:
            logger.error("InterviewSession %s not found", session_id)
            raise
    elif interview_id:
        try:
            interview = Interview.objects.get(pk=interview_id)
            session, _ = InterviewSession.objects.get_or_create(interview=interview)
        except Interview.DoesNotExist:
            logger.error("Interview %s not found", interview_id)
            raise
    else:
        raise ValueError("Payload must contain either interview_id or session_id.")

    session.transcript = transcript_text
    session.save(update_fields=["transcript"])

    interview.status = Interview.Status.DONE
    interview.save(update_fields=["status", "updated_at"])

    result = {
        "session_id": str(session.id),
        "transcript": session.transcript,
    }

    logger.info("process_retell_transcript completed for session_id=%s", session.id)

    # Trigger next stage: behavioral analysis
    from .analysis import aggregate_behavioral
    aggregate_behavioral.delay(session_id=str(session.id))

    return result
