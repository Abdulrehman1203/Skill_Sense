"""Explicit DEBUG-only scaffolding for the owned start-now test action.

Caller holds the Interview row lock. Never approve recruiter drafts or call an
external generator here; use the existing local template/fallback selection.
"""
from django.conf import settings

from .audit import record_audit
from .integrations.gemini_client import get_template_questions
from .models import Interview, Question


def prepare_development_questions_locked(interview, actor):
    if not settings.DEBUG or interview.status != Interview.Status.SCHEDULED or interview.retell_session_id:
        return []
    approved = list(interview.questions.filter(approved=True).order_by('created_at', 'pk'))
    if approved:
        return approved
    templates = get_template_questions()
    selected, seen = [], set()
    # Bounded, balanced prompts; a missing template seed is handled by the
    # existing local emergency templates. No Gemini request or Celery dependency.
    for category in Question.Category.values:
        count = 0
        for item in templates:
            text = item['text'].strip()
            if item['category'] != category or not text or text in seen:
                continue
            selected.append(Question(interview=interview, text=text,
                category=category, source=Question.Source.TEMPLATE, approved=True))
            seen.add(text)
            count += 1
            if count == 2:
                break
    if not selected:
        return []
    Question.objects.bulk_create(selected)
    record_audit(actor=actor, action='interview.development_questions_prepared',
                 target=interview, changed_fields=['questions'])
    return list(interview.questions.filter(approved=True).order_by('created_at', 'pk'))
