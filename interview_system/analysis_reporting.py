"""Revisioned B6 reports and coalesced durable channel-notification intent.

No scoring dispatch. Recovery executes the existing aggregate task directly on
the normal worker/management path, avoiding a growing publication queue.
"""
import asyncio
from datetime import timedelta
import json
import logging
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.db.models import F
from django.utils import timezone

from .analysis_aggregation import calculate, iso
from .models import BehavioralAnalysis, InterviewSession, User
from .socket_auth import SocketAuthorizationError, can_observe, can_publish
from .video_lifecycle import (ACTIVE_JOBS, coverage_locked, drain_locked,
    finalization_write_lock, locked_session)

logger = logging.getLogger(__name__)
SNAPSHOT_COUNTS = ('received', 'admitted', 'processed', 'dropped', 'superseded', 'failed', 'expired', 'valid_gaze', 'unknown_gaze')


def _authorize(session, user_id, recruiter_only=False):
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        raise SocketAuthorizationError('access_denied') from None
    if not (can_observe(user, session) or (not recruiter_only and can_publish(user, session))):
        raise SocketAuthorizationError('access_denied')
    return user


def _snapshot(session, report):
    return {'type': 'analysis.update', 'session_id': str(session.pk), 'revision': report.revision,
            'server_ts': report.report['generated_at'], 'status': report.report['status'],
            'coverage': {name: report.coverage[name] for name in SNAPSHOT_COUNTS},
            'observations': report.report['observations']}


def aggregate_session(session_id):
    with locked_session(session_id) as (_, session):
        if session.lifecycle_version != 1 or not session.video_required:
            return {'status': 'unavailable', 'reason': 'legacy_unverified' if session.video_required else 'video_disabled'}
        drain_locked(session)
        old = BehavioralAnalysis.objects.select_for_update().filter(session=session).first()
        if old and old.report_schema_version != 1:
            session.report_dirty = False
            session.save(update_fields=['report_dirty'])
            return {'status': 'unavailable', 'reason': 'legacy_report_conflict'}
        if old and old.finalized_at is not None:
            return {'status': old.report['status'], 'revision': old.revision}
        if old and not session.report_dirty and old.source_revision == session.analysis_source_revision:
            return {'status': old.report['status'], 'revision': old.revision}
        if session.analysis_finalized_at is not None and (session.analysis_state != 'failed'
                or session.frame_jobs.filter(state='processed').exists()):
            session.report_dirty = False
            session.save(update_fields=['report_dirty'])
            return {'status': 'unavailable', 'reason': 'terminal_report_missing'}
        now = timezone.now()
        if session.finalization_lease_until and session.finalization_lease_until > now:
            return {'status': 'pending', 'reason': 'report_claimed'}
        token = uuid.uuid4()
        session.finalization_token = token
        session.finalization_lease_until = now+timedelta(seconds=settings.INTERVIEW_ANALYSIS_FINALIZATION_CLAIM_SECONDS)
        session.report_due_at = session.finalization_lease_until
        session.save(update_fields=['finalization_token', 'finalization_lease_until', 'report_due_at'])
        source_revision = session.analysis_source_revision
        early_terminal = session.analysis_finalized_at is not None
        base = coverage_locked(session)
    # SQL iterator + pure temporal fold outside parent locks. Changed source
    # revisions/expired claims cannot commit this optimistic result.
    try:
        computed = calculate(session, base)
    except TimeoutError:
        return {'status': 'pending', 'reason': 'stale_report'}
    except ValueError:
        from ai.vision.rules import RuleConfig
        final = session.admission_sealed_at is not None and session.analysis_ready_at is not None
        kinds = ('phone_visible', 'multiple_people_observed', 'no_face_observed', 'gaze_away')
        computed = {'schema_version': 1, 'status': 'failed' if final else 'finalizing'
                    if session.admission_sealed_at else 'processing', 'final': final,
            'reason': 'pipeline_failed', 'warnings': ['timeline_unavailable'],
            'coverage': {**base, 'valid_gaze': 0, 'unknown_gaze': base['processed'],
                         'usable_frames': None, 'unusable_frames': None, 'unsupported_frames': None,
                         'known_seconds': {name: None for name in ('face', 'gaze', 'objects')}},
            'gaze_centered_pct': None, 'episodes': {'rules_version': RuleConfig().version,
                'events': [], 'events_truncated': False, 'episode_counts': {name: None for name in kinds},
                'observed_seconds': {name: None for name in kinds}}, 'versions': {},
            'validation_status': 'real_world_accuracy_outstanding', 'scoring_eligible': False,
            'expression': {'status': 'disabled', 'label': None}, 'observations': [], 'ledger_rows_seen': None}
    if computed['final'] and not early_terminal:
        try:
            with finalization_write_lock(session_id, token) as (current, report):
                return _persist_locked(current, report, computed, source_revision, token)
        except SocketAuthorizationError:
            return {'status': 'pending', 'reason': 'stale_report'}
    try:
        with locked_session(session_id) as (_, current):
            report = BehavioralAnalysis.objects.select_for_update().filter(session=current).first()
            return _persist_locked(current, report, computed, source_revision, token)
    except SocketAuthorizationError:
        return {'status': 'pending', 'reason': 'stale_report'}


def _persist_locked(session, report, computed, source_revision, token):
    now = timezone.now()
    deadline = session.finalization_lease_until
    if (session.finalization_token != token or session.finalization_lease_until is None
            or session.finalization_lease_until <= now
            or session.analysis_source_revision != source_revision
            or (report and (report.report_schema_version != 1 or report.finalized_at is not None))):
        # Preserve the owner's lease; the indexed due pass retries after expiry.
        return {'status': 'pending', 'reason': 'stale_report'}
    if computed['final'] and (session.admission_sealed_at is None or session.analysis_ready_at is None
            or session.frame_jobs.filter(state__in=ACTIVE_JOBS).exists()
            or session.frame_jobs.count() != session.sealed_admission_count):
        raise RuntimeError('Report admission boundary mismatch.')
    # B2's zero-processed terminal state already allocated its final revision.
    revision = session.analysis_revision if session.analysis_finalized_at else session.analysis_revision+1
    report = report or BehavioralAnalysis(session=session)
    report.report_schema_version = 1
    report.revision = revision
    report.source_revision = source_revision
    report.finalized_at = (session.analysis_finalized_at or now) if computed['final'] else None
    report.sealed_admission_count = session.sealed_admission_count
    report.attention_pct = None  # Approximate gaze is not an attention/scoring metric.
    report.look_away_count = None  # Duration episodes are not legacy frame counts.
    report.integrity_flags = []  # No integrity policy applied or automatic verdict.
    report.emotion_summary = {}
    report.pipeline_version = 'aggregate-v1'
    report.model_version = 'source_versions_in_report'
    report.rules_version = computed['episodes']['rules_version']
    report.coverage = computed['coverage']
    report.report = {**computed, 'revision': revision, 'generated_at': iso(now),
                     'finalized_at': iso(report.finalized_at) if report.finalized_at else None}
    # The frozen observer envelope is always bounded and stored before sending.
    while len(json.dumps(_snapshot(session, report), allow_nan=False).encode()) > 65536:
        report.report['observations'].pop(0)
    while len(json.dumps(report.report, allow_nan=False).encode()) > 131072:
        if report.report['observations']:
            report.report['observations'].pop(0)
        elif report.report['episodes']['events']:
            report.report['episodes']['events'].pop(0)
            report.report['episodes']['events_truncated'] = True
        else:
            raise RuntimeError('Report size invariant failed.')
    report.notify_after = now
    report.save()
    session.analysis_revision = revision
    session.analysis_state = computed['status']
    session.report_dirty = False
    session.report_due_at = now+timedelta(seconds=5)
    if not computed['final']:
        session.finalization_lease_until = None
    if computed['final']:
        session.analysis_finalized_at = report.finalized_at
        session.analysis_failure_reason = (computed['reason'] if computed['status'] == 'failed' else '')
    session.save()
    if deadline <= timezone.now():
        raise SocketAuthorizationError('stale_finalization', 409)
    return {'status': computed['status'], 'revision': revision}


def report_response(session_id, user_id, *, snapshot=False):
    with locked_session(session_id) as (_, session):
        _authorize(session, user_id, recruiter_only=True)
        report = BehavioralAnalysis.objects.filter(session=session).first()
        availability = ('disabled' if not session.video_required else 'legacy_unverified'
                        if session.lifecycle_version != 1 or (report and report.report_schema_version != 1)
                        else 'ready' if report else 'pending')
        value = (_snapshot(session, report) if snapshot else report.report) if availability == 'ready' else None
        return {'session_id': str(session.pk), 'availability': availability,
                'revision': report.revision if availability == 'ready' else 0,
                'snapshot' if snapshot else 'report': value}


def status_response(session_id, user_id):
    with locked_session(session_id) as (_, session):
        user = _authorize(session, user_id)
        stored = BehavioralAnalysis.objects.filter(session=session).first()
        report = stored if stored and stored.report_schema_version == 1 else None
        availability = ('disabled' if not session.video_required else 'legacy_unverified'
                        if session.lifecycle_version != 1 or (stored and not report)
                        else 'ready' if report else 'pending')
        value = {'session_id': str(session.pk), 'video_state': session.video_state,
            'connection_state': session.connection_state, 'analysis_state': session.analysis_state,
            'revision': report.revision if report else 0, 'report_ready': bool(report),
            'final': bool(report and report.finalized_at), 'video_required': session.video_required,
            'availability': availability}
        if can_observe(user, session):
            value['coverage'] = report.coverage if report else None
        return value


def deliver_notification(session_id):
    if connection.in_atomic_block:
        # This service is safe even when a management/recovery caller supplies
        # an outer transaction. Nested atomic() exit is not a commit boundary.
        transaction.on_commit(lambda: deliver_notification(session_id))
        return False
    with locked_session(session_id) as (_, session):
        report = BehavioralAnalysis.objects.select_for_update().filter(session=session, report_schema_version=1).first()
        now = timezone.now()
        if (report is None or report.notify_after is None or report.notify_after > now
                or report.notified_revision >= report.revision):
            return False
        revision = report.revision
        report.notify_after = now+timedelta(seconds=10)
        report.save(update_fields=['notify_after'])
    async def send():
        layer = get_channel_layer()
        await asyncio.wait_for(layer.group_send('analysis.observe.'+str(session_id),
            {'type': 'analysis.update', 'session_id': str(session_id), 'revision': revision}), 2)
    try:
        async_to_sync(send)()
    except Exception:
        return False  # Durable next-attempt time, no exception/URL/credential logging.
    with locked_session(session_id) as (_, session):
        report = BehavioralAnalysis.objects.select_for_update().get(session=session)
        report.notified_revision = max(report.notified_revision, revision)
        if report.revision == revision:
            report.notify_after = None
        report.save(update_fields=['notified_revision', 'notify_after'])
    return True


def recover_reports_batch():
    ids = list(InterviewSession.objects.filter(lifecycle_version=1, video_required=True,
        started_at__isnull=False, report_dirty=True, report_due_at__lte=timezone.now())
        .order_by('report_due_at', 'pk').values_list('pk', flat=True)[:settings.INTERVIEW_ANALYSIS_REPORT_RECOVERY_BATCH])
    from .tasks.analysis import aggregate_behavioral
    for session_id in ids:
        try:
            aggregate_behavioral.run(str(session_id))
        except (DatabaseError, RuntimeError, ValueError):
            try:
                with locked_session(session_id) as (_, session):
                    session.report_due_at = timezone.now()+timedelta(seconds=30)
                    session.save(update_fields=['report_due_at'])
            except DatabaseError:
                pass
            logger.warning('Report recovery deferred. session_id=%s', session_id)
    return len(ids)


def recover_notifications_batch():
    ids = list(BehavioralAnalysis.objects.filter(report_schema_version=1, notify_after__lte=timezone.now(),
        notified_revision__lt=F('revision')).order_by('notify_after', 'pk')
        .values_list('session_id', flat=True)[:settings.INTERVIEW_ANALYSIS_REPORT_RECOVERY_BATCH])
    for session_id in ids:
        try:
            deliver_notification(session_id)
        except DatabaseError:
            logger.warning('Report notification recovery deferred. session_id=%s', session_id)
    return len(ids)
