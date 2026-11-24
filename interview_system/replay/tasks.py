import logging
import uuid
from datetime import timedelta
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..integrations.retell_client import RetellUnavailableError, get_call
from ..models import InterviewReplay, ReplayObject
from . import media, storage
from .views import UPLOAD_GRACE

logger = logging.getLogger(__name__)
State = InterviewReplay.State
TASK_LIMIT = 780
LEASE_TIME = timedelta(seconds=TASK_LIMIT + 550)
RETRY_WAIT = timedelta(seconds=60)
MAX_JOB_ATTEMPTS = 10


def finish_locked(replay, state, detail='', *, key='', gaps=None, timing=None):
    now = timezone.now()
    replay.state = state
    replay.detail = detail
    replay.output_key = key
    replay.gaps = gaps or []
    replay.timing = timing or {}
    replay.lease = replay.lease_until = None
    replay.scratch_bytes = 0
    replay.save()
    ReplayObject.objects.filter(replay=replay).exclude(pk=key).update(delete_after=now)
    logger.info('Replay finalized id=%s state=%s reason=%s', replay.pk, state, detail)


def select_job():
    now = timezone.now()
    with transaction.atomic():
        replay = (InterviewReplay.objects.select_for_update(skip_locked=True, of=('self',))
                  .filter(state__in=(State.RECORDING, State.PROCESSING), expires_at__gt=now)
                  .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
                  .filter(Q(due_at__lte=now) |
                          Q(state=State.RECORDING, detail='', call__ended_at__lte=now-UPLOAD_GRACE,
                            last_activity__lte=now-UPLOAD_GRACE))
                  .order_by('due_at').first())
        if not replay:
            return None
        if replay.state == State.RECORDING and now < replay.capture_until + UPLOAD_GRACE:
            next_check = min(replay.last_activity + UPLOAD_GRACE, replay.capture_until + UPLOAD_GRACE)
            if next_check > now:
                replay.due_at = next_check
                replay.save(update_fields=['due_at'])
                return None
        if replay.finalize_until <= now or replay.attempts >= MAX_JOB_ATTEMPTS:
            state = State.FAILED if replay.detail == 'processing_error' else State.UNAVAILABLE
            finish_locked(replay, state, replay.detail or 'reconciliation_exhausted')
            return None
        replay.lease = uuid.uuid4()
        replay.lease_until = now + LEASE_TIME
        replay.save(update_fields=['lease', 'lease_until'])
        return replay


def retry(replay, reason, *, unstarted=False):
    with transaction.atomic():
        current = InterviewReplay.objects.select_for_update().filter(pk=replay.pk, lease=replay.lease).first()
        if not current:
            return
        current.lease = current.lease_until = None
        current.scratch_bytes = 0
        current.due_at = timezone.now() + RETRY_WAIT
        if not unstarted:
            current.detail = reason
        current.save()
        ReplayObject.objects.filter(replay=current, segment__isnull=True, verified=False).update(
            delete_after=timezone.now())
        if not unstarted:
            logger.warning('Replay reconciliation deferred id=%s reason=%s', current.pk, reason)


def begin_processing(replay):
    now = timezone.now()
    with transaction.atomic():
        current = InterviewReplay.objects.select_for_update().filter(
            pk=replay.pk, lease=replay.lease, lease_until__gt=now).first()
        if not current:
            return None
        if current.state == State.RECORDING and now < min(
                current.last_activity + UPLOAD_GRACE, current.capture_until + UPLOAD_GRACE):
            return None
        if not ReplayObject.objects.filter(replay=current, verified=True).exists():
            finish_locked(current, State.UNAVAILABLE, 'no_verified_video')
            return None
        # Reclaim stale reservations only after the previous hard limit has
        # elapsed. Its private output is garbage collected independently.
        current.scratch_bytes = 0
        current.save(update_fields=['scratch_bytes'])
        with storage.budget() as ledger:
            key = f'{storage.PREFIX}{current.pk}/outputs/{current.lease}.mp4'
            scratch = storage.MAX_INPUT + storage.MAX_AUDIO + storage.MAX_OUTPUT
            storage.reserve(ledger, scratch + storage.MAX_OUTPUT)
            ReplayObject.objects.create(pk=key, replay=current, size=storage.MAX_OUTPUT,
                                        delete_after=current.lease_until)
            current.scratch_bytes = scratch
            current.state = State.PROCESSING
            current.attempts += 1
            current.save()
    return key


def wait_for_audio(replay):
    with transaction.atomic():
        current = InterviewReplay.objects.select_for_update().filter(pk=replay.pk, lease=replay.lease).first()
        if not current:
            return
        now = timezone.now()
        if current.state == State.RECORDING and now >= min(
                current.last_activity + UPLOAD_GRACE, current.capture_until + UPLOAD_GRACE):
            current.state = State.PROCESSING
            current.finalize_until = min(current.finalize_until, now + timedelta(minutes=20))
            current.save(update_fields=['state', 'finalize_until'])
    retry(replay, 'audio_pending')


def publish(replay, key, size, digest, gaps, partial, timing):
    with transaction.atomic():
        now = timezone.now()
        current = InterviewReplay.objects.select_for_update().filter(
            pk=replay.pk, lease=replay.lease, state=State.PROCESSING,
            expires_at__gt=now, lease_until__gt=now,
        ).first()
        if not current:
            return
        updated = ReplayObject.objects.filter(pk=key, replay=current, verified=False).update(
            size=size, digest=digest, verified=True, delete_after=current.expires_at)
        if not updated:
            return
        finish_locked(current, State.PARTIAL if partial else State.READY,
                      'video_gaps' if partial else '', key=key, gaps=gaps, timing=timing)


def finalize(replay, s3):
    try:
        # The existing integration checks that Retell returned the requested
        # call ID. Polling covers delayed and missed authenticated webhooks.
        call = get_call(replay.call_id)
        if call.get('call_status') in ('error', 'not_connected'):
            with transaction.atomic():
                current = InterviewReplay.objects.select_for_update().filter(pk=replay.pk, lease=replay.lease).first()
                if current:
                    finish_locked(current, State.UNAVAILABLE, 'call_unavailable')
            return
        if call.get('call_status') != 'ended':
            retry(replay, 'audio_pending', unstarted=True)
            return
        if not (call.get('recording_multi_channel_url') or call.get('recording_url')):
            wait_for_audio(replay)
            return
        key = begin_processing(replay)
        if key is None:
            retry(replay, 'waiting_for_upload', unstarted=True)
            return
        with storage.scratch_space() as scratch:
            output, digest, gaps, partial, timing = media.assemble(replay, call, s3, Path(scratch))
            size = output.stat().st_size
            storage.put_output(s3, key, output, size, digest)
        publish(replay, key, size, digest, gaps, partial, timing)
    except storage.QuotaExceeded:
        retry(replay, 'quota_exhausted')
    except media.MediaUnavailable as error:
        retry(replay, str(error) or 'audio_pending')
    except RetellUnavailableError:
        retry(replay, 'audio_pending')
    except media.Unaligned:
        with transaction.atomic():
            current = InterviewReplay.objects.select_for_update().filter(pk=replay.pk, lease=replay.lease).first()
            if current:
                finish_locked(current, State.UNAVAILABLE, 'alignment_unverified')
    except (media.InvalidMedia, BotoCoreError, ClientError, OSError, ValueError):
        retry(replay, 'processing_error')


def cleanup(s3):
    now = timezone.now()
    expired = InterviewReplay.objects.filter(expires_at__lte=now).exclude(state=State.EXPIRED)
    for pk in expired.order_by('expires_at').values_list('pk', flat=True)[:20]:
        with transaction.atomic():
            replay = InterviewReplay.objects.select_for_update().filter(pk=pk).exclude(state=State.EXPIRED).first()
            if replay and (replay.lease_until is None or replay.lease_until <= now):
                finish_locked(replay, State.EXPIRED)

    keys = (ReplayObject.objects.filter(Q(delete_after__lte=timezone.now()) | Q(replay__isnull=True))
            .order_by('delete_after').values_list('pk', flat=True)[:50])
    with storage.budget():
        try:
            objects = [{'Key': key} for key in keys]
            if objects:
                result = s3.delete_objects(Bucket=settings.REPLAY_R2_BUCKET, Delete={'Objects': objects})
                ReplayObject.objects.filter(pk__in=[obj['Key'] for obj in result.get('Deleted', [])]).delete()
                if result.get('Errors'):
                    logger.warning('Replay object cleanup incomplete count=%s', len(result['Errors']))
        except (BotoCoreError, ClientError):
            logger.warning('Replay object cleanup deferred')


@shared_task(name='interview_system.replay.reconcile', ignore_result=True,
             acks_late=True, reject_on_worker_lost=True, time_limit=TASK_LIMIT)
def reconcile():
    if not storage.configured():
        return
    s3 = storage.client()
    storage.clean_stale_scratch(LEASE_TIME.total_seconds())
    cleanup(s3)
    replay = select_job()
    if replay:
        finalize(replay, s3)
