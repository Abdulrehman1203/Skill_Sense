"""
Django signals for interview_system.

Wired up in InterviewSystemConfig.ready() — do NOT import this module
at the top of models.py or anywhere that triggers on import.
"""

from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Application



@receiver(post_save, sender=Application)
def enqueue_resume_parsing(
    sender: type,
    instance: Application,
    created: bool,
    **kwargs,
) -> None:
    """
    Persist an application assessment and dispatch it after commit.

    Fires only on creation (created=True), NOT on subsequent saves
    (e.g. status advances via /advance/ endpoint).

    Uses transaction.on_commit to ensure the database transaction is committed
    before the worker attempts to fetch the records, preventing race conditions.
    """
    if not created:
        return

    from .screening import create_assessment
    create_assessment(instance.pk)


