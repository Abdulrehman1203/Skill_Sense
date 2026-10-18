"""Bounded lifecycle/dispatch recovery. No inference or fabricated reports."""
from celery import shared_task
from ..video_lifecycle import recover_video_batch


@shared_task(ignore_result=True)
def recover_video():
    return recover_video_batch()
