"""
Django signals for interview_system.

Wired up in InterviewSystemConfig.ready() — do NOT import this module
at the top of models.py or anywhere that triggers on import.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Application
from .tasks import parse_resume

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Application)
def enqueue_resume_parsing(
    sender: type,
    instance: Application,
    created: bool,
    **kwargs,
) -> None:
    """
    Enqueue the parse_resume Celery task when a new Application is created.

    Fires only on creation (created=True), NOT on subsequent saves
    (e.g. status advances via /advance/ endpoint).

    Uses transaction.on_commit to ensure the database transaction is committed
    before the worker attempts to fetch the records, preventing race conditions.
    """
    if not created:
        return

    resume_id = str(instance.resume_id)
    logger.info(
        "Application %s created — enqueuing parse_resume for resume %s on commit",
        instance.pk,
        resume_id,
    )
    transaction.on_commit(
        lambda: parse_resume.delay(resume_id=resume_id)  # type: ignore[attr-defined]
    )


