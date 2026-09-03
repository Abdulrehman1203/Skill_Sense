"""
Celery application configuration for SkillSense.

Broker and result backend both point to the project's Redis instance.
Tasks are auto-discovered from each INSTALLED_APP's ``tasks.py`` module.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ai_interview_recruitment.settings")

app = Celery("ai_interview_recruitment")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
