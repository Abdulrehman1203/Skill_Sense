"""Durable dispatch outside ASGI. B2 parent/session/job lock order throughout."""
from datetime import timedelta
import uuid
from django.conf import settings
from django.utils import timezone
from redis.exceptions import RedisError
from .models import FrameJob
from .video_lifecycle import (TERMINAL_JOBS, claim_frame, drain_locked,
                              locked_job, locked_session, settle_frame, touch_analysis_locked)
from .vision_payloads import PayloadStore
from . import vision_inference


def terminal_locked(session, job, state, reason):
    job.state = state
    job.terminal_at = timezone.now()
    job.terminal_reason = reason
    job.save()
    touch_analysis_locked(session)
    drain_locked(session)


def dispatch_session(session_id):
    with locked_session(session_id) as (_, session):
        now = timezone.now()
        job = session.frame_jobs.select_for_update().filter(state='processing').first()
        if job and job.claim_expires_at > now:
            return False
        job = job or session.frame_jobs.select_for_update().filter(state='pending').first()
        if job is None or not job.payload_ready:
            return False
        if job.expires_at <= now:
            terminal_locked(session, job, 'expired', 'deadline_expired')
            return False
        if job.publication_due_at > now:
            return False
        if job.publication_attempts >= settings.INTERVIEW_ANALYSIS_PUBLICATION_ATTEMPTS:
            terminal_locked(session, job, 'failed', 'publication_failed')
            return False
        if job.attempts >= settings.INTERVIEW_ANALYSIS_WORKER_ATTEMPTS:
            terminal_locked(session, job, 'failed', 'worker_failed')
            return False
        job.publication_token = uuid.uuid4()
        job.publication_attempts += 1
        # Covers publisher death after commit and ambiguous broker success.
        job.publication_due_at = now + timedelta(seconds=settings.INTERVIEW_ANALYSIS_PUBLICATION_LEASE_SECONDS)
        job.save()
        job_id, token, deadline = str(job.pk), str(job.publication_token), job.expires_at
    try:
        from .tasks.vision import process_frame
        if settings.CELERY_TASK_ALWAYS_EAGER or process_frame.app.conf.task_always_eager:
            return False  # Never execute vision inline in a recovery/control process.
        process_frame.apply_async(args=[job_id, token], queue=settings.INTERVIEW_ANALYSIS_VISION_QUEUE,
            routing_key=settings.INTERVIEW_ANALYSIS_VISION_QUEUE, priority=0, expires=deadline,
            retry=False, ignore_result=True)
        return True
    except Exception:
        # Committed due time/attempt cap is the outbox; no per-retry outbox rows.
        # Broker exception strings can contain credentials, so never log them.
        return False


def execute_frame(job_id, publication_token):
    try:
        publication_token = uuid.UUID(str(publication_token))
        with locked_job(job_id) as (_, session, job):
            if (job.publication_token != publication_token or not job.payload_ready
                    or job.state in TERMINAL_JOBS or job.attempts >= settings.INTERVIEW_ANALYSIS_WORKER_ATTEMPTS):
                return
            token = claim_frame(job.pk)
            if token is None:
                return
    except (FrameJob.DoesNotExist, ValueError):
        return
    store = PayloadStore()
    try:
        data = store.get(job_id)
        if data is None:
            settle_frame(job_id, token, 'failed', reason='payload_missing')
        else:
            result, versions = (vision_inference.analyze_jpeg(data, calibration=session.vision_calibration)
                                if session.vision_calibration else vision_inference.analyze_jpeg(data))
            settle_frame(job_id, token, 'processed', result=result, versions=versions)
    except (RedisError, vision_inference.TransientInferenceError):
        with locked_job(job_id) as (_, session, current):
            if (current.state == 'processing' and current.claim_token == token
                    and current.publication_token == publication_token
                    and current.claim_expires_at > timezone.now() and current.expires_at > timezone.now()):
                if current.attempts >= settings.INTERVIEW_ANALYSIS_WORKER_ATTEMPTS:
                    terminal_locked(session, current, 'failed', 'worker_failed')
                else:
                    current.claim_expires_at = timezone.now()
                    current.publication_due_at = timezone.now() + timedelta(seconds=2)
                    current.save()
    except Exception:
        # Unavailable B5 and invalid/corrupt input/output are permanent failures.
        settle_frame(job_id, token, 'failed', reason='inference_failed')
    finally:
        try:
            # Never delete a payload that is still owned by a newer retry.
            if FrameJob.objects.filter(pk=job_id, state__in=TERMINAL_JOBS).exists():
                store.delete(job_id)
        except RedisError:
            pass
        store.close()
    dispatch_session(job.session_id)  # Scanner supplies liveness if this fails.


def recover_frames(session_id):
    return dispatch_session(session_id)
