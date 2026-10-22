"""B2 transactional metadata/lifecycle. No image bytes, inference, or broker I/O.

Lock order: admission gate (admission only) -> Interview -> InterviewSession ->
FrameJob -> FrameAnalysis/BehavioralAnalysis. Never acquire the gate inside a
parent lock. All public mutations take their own parent/session locks.
"""
from contextlib import contextmanager
from datetime import timedelta
import math
import logging
import uuid

from channels.db import database_sync_to_async
from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from .models import (BehavioralAnalysis, FrameAdmissionGate, FrameAnalysis, FrameJob,
                     Interview, InterviewSession, RetellWebhookEvent, User)
from .session_completion import finish_if_ready
from .socket_auth import SocketAuthorizationError, can_publish, check_lease

ACTIVE_JOBS = ('pending', 'processing')
TERMINAL_JOBS = ('processed', 'superseded', 'failed', 'expired')
FINISH_REASONS = ('candidate_finished', 'audio_ended', 'reconnect_timeout',
                  'inactivity_timeout', 'duration_limit', 'service_failure')
logger = logging.getLogger(__name__)


def seconds(name):
    return timedelta(seconds=max(1, getattr(settings, name)))


@contextmanager
def locked_session(session_id):
    with transaction.atomic():
        try:
            parent_id = InterviewSession.objects.values_list('interview_id', flat=True).get(pk=session_id)
            interview = Interview.objects.select_for_update().get(pk=parent_id)
            session = InterviewSession.objects.select_for_update().get(pk=session_id)
        except (InterviewSession.DoesNotExist, Interview.DoesNotExist):
            raise SocketAuthorizationError('access_denied') from None
        session.interview = interview
        yield interview, session


def initialize_video_locked(session):
    """Caller owns parent/session locks; never adopt historical version-0 rows."""
    if session.lifecycle_version != 1 or not session.video_required or session.started_at is None:
        return
    if session.video_activated_at is None:
        session.video_activated_at = session.started_at
        session.video_deadline = session.started_at + seconds('INTERVIEW_ANALYSIS_MAX_VIDEO_SECONDS')
        session.recovery_due_at = timezone.now()
        session.save(update_fields=['video_activated_at', 'video_deadline', 'recovery_due_at'])


def policy_reason(session, now):
    if session.audio_ended_at is not None:
        return 'audio_ended'
    deadlines = []
    if session.video_deadline is not None:
        deadlines.append((session.video_deadline, 0, 'duration_limit'))
    if session.reconnect_deadline is not None:
        deadlines.append((session.reconnect_deadline, 1, 'reconnect_timeout'))
    elif session.connection_state == 'connected' and session.connection_lease_until is not None:
        deadlines.append((session.connection_lease_until + seconds('INTERVIEW_ANALYSIS_RECONNECT_GRACE_SECONDS'), 1, 'reconnect_timeout'))
    baseline = session.last_frame_at or session.video_activated_at
    if baseline is not None:
        deadlines.append((baseline + seconds('INTERVIEW_ANALYSIS_VIDEO_INACTIVITY_SECONDS'), 2, 'inactivity_timeout'))
    due = sorted(d for d in deadlines if d[0] <= now)
    return due[0][2] if due else None


def require_open(session, now):
    if (session.lifecycle_version != 1 or not session.video_required or session.started_at is None
            or session.admission_sealed_at is not None or session.ended_at is not None
            or session.interview.status != Interview.Status.LIVE or policy_reason(session, now)):
        raise SocketAuthorizationError('session_not_publishable', 409)


def _publisher(session, lease, generation=None):
    if lease.purpose != 'publisher' or lease.session_id != session.pk:
        raise SocketAuthorizationError('access_denied')
    check_lease(lease)
    # Account/ownership reads can cross the credential boundary. Use one fresh
    # timestamp so equal auth/transport deadlines retain the auth-expiry reason.
    now = timezone.now()
    if now >= lease.expires_at:
        raise SocketAuthorizationError('lease_expired', 401)
    if generation is not None and (session.connection_generation != generation
            or session.connection_state != 'connected' or session.connection_lease_until is None
            or session.connection_auth_expires_at is None
            or session.connection_auth_expires_at <= now
            or session.connection_lease_until <= now):
        raise SocketAuthorizationError('stale_connection', 409)


def acquire_publisher(lease):
    with locked_session(lease.session_id) as (_, session):
        _publisher(session, lease)
        initialize_video_locked(session)
        now = timezone.now()
        require_open(session, now)
        if (session.connection_state == 'connected' and session.connection_lease_until
                and session.connection_lease_until > now):
            raise SocketAuthorizationError('publisher_conflict', 409)
        generation = uuid.uuid4()
        session.connection_generation = generation
        session.connection_state = 'connected'
        session.video_state = 'active'
        session.connection_auth_expires_at = lease.expires_at
        session.connection_lease_until = min(lease.expires_at, now + seconds('INTERVIEW_ANALYSIS_CONNECTION_LEASE_SECONDS'))
        session.reconnect_deadline = None
        session.last_sequence = None
        session.save()
        return generation


def heartbeat_publisher(lease, generation):
    with locked_session(lease.session_id) as (_, session):
        _publisher(session, lease, generation)
        now = timezone.now()
        require_open(session, now)
        session.connection_lease_until = min(lease.expires_at, session.connection_auth_expires_at,
                                             now + seconds('INTERVIEW_ANALYSIS_CONNECTION_LEASE_SECONDS'))
        session.save(update_fields=['connection_lease_until'])
        return session.connection_lease_until


def disconnect_publisher(session_id, generation):
    # Generation, not still-valid credentials, authorizes this cleanup callback.
    with locked_session(session_id) as (_, session):
        if (session.connection_generation != generation or session.connection_state != 'connected'
                or session.admission_sealed_at is not None):
            return False
        _disconnect_locked(session, timezone.now())
        return True


def _disconnect_locked(session, now):
    anchor = min(now, session.connection_lease_until or now)
    session.connection_state = 'disconnected'
    session.video_state = 'disconnected'
    session.reconnect_deadline = anchor + seconds('INTERVIEW_ANALYSIS_RECONNECT_GRACE_SECONDS')
    session.save(update_fields=['connection_state', 'video_state', 'reconnect_deadline'])


def coverage_locked(session):
    counts = {s: 0 for s in TERMINAL_JOBS}
    counts.update({row['state']: row['n'] for row in session.frame_jobs.values('state').annotate(n=Count('pk'))})
    valid = FrameAnalysis.objects.filter(job__session=session, job__state='processed',
                                         gaze_direction__in=['centered', 'away']).count()
    return {'received': session.received_count, 'admitted': session.admitted_count,
            'dropped': session.dropped_count, **{s: counts[s] for s in TERMINAL_JOBS},
            'valid_gaze': valid, 'unknown_gaze': counts['processed'] - valid}


def touch_analysis_locked(session):
    """Dirty the coalesced B6 report intent under the existing parent/session lock."""
    session.analysis_source_revision += 1
    session.report_dirty = True
    session.save(update_fields=['analysis_source_revision', 'report_dirty'])


def drain_locked(session):
    """Only sealed terminal jobs cross this barrier. No task-order assumptions."""
    if session.admission_sealed_at is None or session.analysis_finalized_at is not None:
        return False
    if session.frame_jobs.filter(state__in=ACTIVE_JOBS).exists():
        return False
    if session.frame_jobs.count() != session.sealed_admission_count:
        raise RuntimeError('Admission boundary mismatch.')
    now = timezone.now()
    session.video_state = 'ended'
    first_ready = session.analysis_ready_at is None
    session.analysis_ready_at = session.analysis_ready_at or now
    counts = coverage_locked(session)
    if counts['processed'] == 0:
        # Explicit terminal unavailability, with no fabricated BehavioralAnalysis row.
        session.analysis_state = 'failed'
        session.analysis_failure_reason = 'no_usable_evidence'
        session.analysis_finalized_at = now
        session.analysis_revision += 1
    # Usable processed frames remain finalizing for B6's real aggregate writer.
    session.save()
    if first_ready:
        touch_analysis_locked(session)
    return True


def seal_video_locked(interview, session, reason):
    """Caller owns parent/session. First seal/reason/boundary is immutable."""
    if session.lifecycle_version != 1 or not session.video_required:
        return session
    if reason not in FINISH_REASONS:
        raise ValueError('Invalid finish reason.')
    if session.admission_sealed_at is None:
        now = timezone.now()
        session.admission_sealed_at = now
        session.sealed_admission_count = session.admitted_count
        session.finish_reason = policy_reason(session, now) or reason
        session.ended_at = session.ended_at or now
        session.connection_state = 'closed'
        session.connection_lease_until = None
        session.reconnect_deadline = None
        session.video_state = 'finalizing'
        session.analysis_state = 'finalizing'
        session.save()
        touch_analysis_locked(session)
    drain_locked(session)
    finish_if_ready(interview, session)
    return session


def finish_video(session_id, user_id):
    with locked_session(session_id) as (interview, session):
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            raise SocketAuthorizationError('access_denied') from None
        if not can_publish(user, session):
            raise SocketAuthorizationError('access_denied')
        if not session.video_required:
            raise SocketAuthorizationError('video_not_enabled', 409)
        if (session.lifecycle_version != 1 or session.started_at is None or
                (session.admission_sealed_at is None and interview.status != Interview.Status.LIVE)):
            raise SocketAuthorizationError('session_not_publishable', 409)
        seal_video_locked(interview, session, 'candidate_finished')
        return {'session_id': str(session.pk), 'video_state': session.video_state, 'finish_reason': session.finish_reason}


def admit_frame(lease, generation, sequence, capture_ts, *, retain_payload=None, received_at=None):
    """Internal B3/B4 metadata boundary, after byte/image/rate validation.

    B4's optional private payload retention hook runs inside the admission lock.
    It must raise on storage failure so replacement/counters roll back together.
    Broker I/O never belongs in this hook. Tests may use metadata-only B2 services.
    Returns (job, None) or (None, drop_reason); retries return the same job.
    """
    if type(sequence) is not int or not 0 <= sequence <= 9007199254740991:
        raise SocketAuthorizationError('invalid_sequence', 400)
    if type(capture_ts) not in (int, float) or not math.isfinite(capture_ts):
        raise SocketAuthorizationError('invalid_frame', 400)
    with transaction.atomic():
        FrameAdmissionGate.objects.get_or_create(pk=1)
        FrameAdmissionGate.objects.select_for_update().get(pk=1)
        with locked_session(lease.session_id) as (_, session):
            _publisher(session, lease, generation)
            now = timezone.now()
            require_open(session, now)
            existing = session.frame_jobs.filter(generation=generation, sequence=sequence).first()
            if existing:
                return existing, None
            if session.last_sequence is not None and sequence <= session.last_sequence:
                raise SocketAuthorizationError('invalid_sequence', 400)
            pending = session.frame_jobs.filter(state='pending').first()
            # Replacement does not increase nonterminal global occupancy.
            full = FrameJob.objects.filter(state__in=ACTIVE_JOBS).count() >= max(1, settings.INTERVIEW_ANALYSIS_GLOBAL_FRAME_CAP)
            session.received_count += 1
            session.last_sequence = sequence
            # Camera liveness is independent of model/queue admission capacity.
            session.last_frame_at = now
            if (full and pending is None) or session.admitted_count >= settings.INTERVIEW_ANALYSIS_MAX_SESSION_FRAMES:
                session.dropped_count += 1
                session.save()
                touch_analysis_locked(session)
                return None, 'capacity'
            if pending:
                pending.state = 'superseded'; pending.terminal_at = now; pending.terminal_reason = 'superseded'
                pending.save(update_fields=['state', 'terminal_at', 'terminal_reason'])
            session.admitted_count += 1
            session.last_frame_at = now
            session.analysis_state = 'processing'
            job = FrameJob.objects.create(session=session, generation=generation, sequence=sequence,
                admission_number=session.admitted_count, capture_ts=capture_ts, received_at=received_at or now,
                expires_at=min(now, received_at or now) + seconds('INTERVIEW_ANALYSIS_FRAME_DEADLINE_SECONDS'))
            if retain_payload is not None:
                # Executed under gate/parent locks. Failure rolls back replacement and counters.
                retain_payload(job, pending)
                job.payload_ready = True
                job.save(update_fields=['payload_ready'])
            session.save()
            touch_analysis_locked(session)
            return job, None


def record_frame_drop(lease, generation, sequence, *, validated=False):
    """Count transport/availability drops once, without creating a pending job."""
    if type(sequence) is not int or not 0 <= sequence <= 9007199254740991:
        raise SocketAuthorizationError('invalid_sequence', 400)
    with locked_session(lease.session_id) as (_, session):
        _publisher(session, lease, generation)
        require_open(session, timezone.now())
        if session.last_sequence is not None and sequence <= session.last_sequence:
            return False  # B4 may already have counted this admission attempt.
        session.last_sequence = sequence
        session.received_count += 1
        session.dropped_count += 1
        fields = ['last_sequence', 'received_count', 'dropped_count']
        if validated:
            session.last_frame_at = timezone.now()
            fields.append('last_frame_at')
        session.save(update_fields=fields)
        touch_analysis_locked(session)
        return True


@contextmanager
def locked_job(job_id):
    session_id = FrameJob.objects.values_list('session_id', flat=True).get(pk=job_id)
    with locked_session(session_id) as (interview, session):
        job = FrameJob.objects.select_for_update().get(pk=job_id)
        yield interview, session, job


def claim_frame(job_id):
    with locked_job(job_id) as (_, session, job):
        now = timezone.now()
        if (session.analysis_finalized_at is not None or job.state in TERMINAL_JOBS or job.expires_at <= now
                or (job.state == 'processing' and job.claim_expires_at > now)):
            return None
        if session.frame_jobs.filter(state='processing').exclude(pk=job.pk).exists():
            return None
        job.state = 'processing'
        job.claim_token = uuid.uuid4()
        job.claim_expires_at = min(job.expires_at, now + seconds('INTERVIEW_ANALYSIS_FRAME_CLAIM_SECONDS'))
        job.attempts += 1
        job.save()
        return job.claim_token


def _validate_observation(result, versions):
    # A persistence interface for future real workers; never an inference implementation.
    required = {'gaze_direction', 'face_count', 'detected_objects'}
    if not isinstance(result, dict) or not required <= set(result) or set(result)-required-{'vision_details'}:
        raise ValueError('Invalid observation schema.')
    if result['gaze_direction'] not in ('unknown', 'centered', 'away'):
        raise ValueError('Invalid gaze observation.')
    if (result['face_count'] is None and 'vision_details' not in result) or (result['face_count'] is not None
            and (type(result['face_count']) is not int or not 0 <= result['face_count'] <= 100)):
        raise ValueError('Invalid face count.')
    objects = result['detected_objects']
    if not isinstance(objects, list) or len(objects) > 20:
        raise ValueError('Invalid object observations.')
    for item in objects:
        if not isinstance(item, dict) or set(item) != {'label', 'confidence', 'box'}:
            raise ValueError('Invalid object observation.')
        if item['label'] not in ('person', 'phone'):
            raise ValueError('Invalid object label.')
        values = [item['confidence']] + (item['box'] if isinstance(item['box'], list) else [])
        if (len(values) != 5 or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in values)
                or item['box'][0] + item['box'][2] > 1 or item['box'][1] + item['box'][3] > 1):
            raise ValueError('Invalid object bounds.')
    if set(versions) != {'pipeline_version', 'model_version', 'rules_version'} or any(
            not isinstance(v, str) or not v or len(v) > 100 for v in versions.values()):
        raise ValueError('Missing observation provenance.')
    if 'vision_details' in result:
        from ai.vision.schema import validate_details
        validate_details(result['vision_details'], result)
    elif versions['pipeline_version'] == 'vision-v1':
        raise ValueError('Vision pipeline requires quality/availability provenance.')


def settle_frame(job_id, claim_token, outcome, *, result=None, versions=None, reason='worker_failed'):
    if outcome not in ('processed', 'failed', 'expired'):
        raise ValueError('Invalid terminal outcome.')
    if reason not in ('worker_failed', 'inference_failed', 'payload_missing', 'deadline_expired'):
        raise ValueError('Invalid terminal reason.')
    if outcome == 'processed':
        _validate_observation(result, versions or {})
    with locked_job(job_id) as (_, session, job):
        # A terminal job cannot be rewritten even by the same claim/retry.
        if job.state in TERMINAL_JOBS:
            return job.state == outcome and job.claim_token == claim_token
        now = timezone.now()
        if (session.analysis_finalized_at is not None or job.state != 'processing' or job.claim_token != claim_token
                or job.claim_expires_at <= now or job.expires_at <= now):
            return False
        if outcome == 'processed':
            FrameAnalysis.objects.create(job=job, session=session, ts=job.received_at, **result, **versions)
        job.state = outcome; job.terminal_at = now
        job.terminal_reason = '' if outcome == 'processed' else reason
        job.save()
        touch_analysis_locked(session)
        drain_locked(session)
        return True


def claim_finalization(session_id):
    with locked_session(session_id) as (_, session):
        drain_locked(session)
        now = timezone.now()
        if (session.analysis_ready_at is None or session.analysis_finalized_at is not None
                or (session.finalization_lease_until and session.finalization_lease_until > now)):
            return None
        session.finalization_token = uuid.uuid4()
        session.finalization_lease_until = now + seconds('INTERVIEW_ANALYSIS_FINALIZATION_CLAIM_SECONDS')
        session.save(update_fields=['finalization_token', 'finalization_lease_until'])
        return session.finalization_token


@contextmanager
def finalization_write_lock(session_id, token):
    """B6 must persist its real aggregate inside this context, not after it."""
    with locked_session(session_id) as (_, session):
        if (token is None or session.finalization_token != token or session.finalization_lease_until is None
                or session.finalization_lease_until <= timezone.now() or session.analysis_finalized_at is not None
                or session.analysis_ready_at is None or session.admission_sealed_at is None
                or session.frame_jobs.filter(state__in=ACTIVE_JOBS).exists()
                or session.frame_jobs.count() != session.sealed_admission_count):
            raise SocketAuthorizationError('stale_finalization', 409)
        # Serializes against any existing report; B6 must not overwrite legacy provenance.
        analysis = BehavioralAnalysis.objects.select_for_update().filter(session=session).first()
        yield session, analysis
        if session.finalization_lease_until <= timezone.now():
            # Roll back an aggregate write that held the DB transaction past its lease.
            raise SocketAuthorizationError('stale_finalization', 409)


def recover_video_session(session_id):
    # Recover the existing signature-verified durable inbox even if per-event
    # broker publication failed. Use its single canonical application service,
    # before taking our locks so parent/session/inbox ordering stays consistent.
    call_id = InterviewSession.objects.filter(pk=session_id, lifecycle_version=1, video_required=True).values_list(
        'interview__retell_session_id', flat=True).first()
    if call_id:
        from .voice import apply_event
        events = list(RetellWebhookEvent.objects.filter(call_id=call_id, processed_at__isnull=True)
                      .order_by('received_at', 'pk').values_list('pk', flat=True)[:2])
        for event_id in events:
            try:
                apply_event(event_id)
            except LookupError:
                logger.warning('Video recovery deferred unlinked/ambiguous Retell event. event_id=%s', event_id)
    with locked_session(session_id) as (interview, session):
        if session.lifecycle_version != 1 or not session.video_required or session.started_at is None:
            return False
        initialize_video_locked(session)
        now = timezone.now()
        session.recovery_due_at = now + seconds('INTERVIEW_ANALYSIS_VIDEO_RECOVERY_SECONDS')
        session.save(update_fields=['recovery_due_at'])
        expired = session.frame_jobs.filter(state__in=ACTIVE_JOBS, expires_at__lte=now).update(
            state='expired', terminal_at=now, terminal_reason='deadline_expired')
        if expired:
            touch_analysis_locked(session)
        if session.admission_sealed_at is None:
            if (session.connection_state == 'connected' and session.connection_lease_until
                    and session.connection_lease_until <= now):
                _disconnect_locked(session, now)
            reason = policy_reason(session, now)
            if reason:
                seal_video_locked(interview, session, reason)
        drain_locked(session)
        return True


def recover_video_batch():
    now = timezone.now()
    ids = list(InterviewSession.objects.filter(lifecycle_version=1, video_required=True,
        started_at__isnull=False, analysis_finalized_at__isnull=True, recovery_due_at__lte=now)
        .order_by('recovery_due_at', 'pk').values_list('pk', flat=True)[:max(1, settings.INTERVIEW_ANALYSIS_VIDEO_RECOVERY_BATCH)])
    for session_id in ids:
        recover_video_session(session_id)
        from .vision_scheduling import recover_frames
        recover_frames(session_id)
    from .analysis_reporting import recover_reports_batch, recover_notifications_batch
    recover_reports_batch()
    recover_notifications_batch()
    return len(ids)


acquire_publisher_async = database_sync_to_async(acquire_publisher)
heartbeat_publisher_async = database_sync_to_async(heartbeat_publisher)
disconnect_publisher_async = database_sync_to_async(disconnect_publisher)
admit_frame_async = database_sync_to_async(admit_frame)
