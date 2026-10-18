"""Explicit reference-only task. Early ack; PostgreSQL owns loss recovery."""
from celery import shared_task


@shared_task(ignore_result=True, acks_late=False, reject_on_worker_lost=False,
             max_retries=0, soft_time_limit=20, time_limit=25)
def process_frame(job_id, publication_token):
    from ..vision_scheduling import execute_frame
    execute_frame(job_id, publication_token)


@shared_task(ignore_result=True, acks_late=False, max_retries=0, soft_time_limit=20, time_limit=25)
def warm_vision():
    from ..vision_inference import ensure_warm, close_runtime
    try:
        ensure_warm()
    except Exception:
        close_runtime()  # Readiness fails closed; periodic warm task can recover.
