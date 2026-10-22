"""Durable combined-v1 scoring; legacy public task name retained as an adapter."""
from datetime import timedelta
import uuid

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ai.scoring.rubric import combined_score
from ..answer_evaluation import digest
from ..final_scoring import current_inputs, dispatch, ensure, lock_sources, payload
from ..models import Application, CandidateScore, Notification, ScoreRevision, ScoringPolicyState


class ScoringConfigurationError(RuntimeError):
    """Compatibility import; configuration gaps are now incomplete states."""


class ScoringInputNotReady(RuntimeError):
    """Compatibility import; missing evidence is now explicit, never zero."""


@shared_task(name='interview_system.tasks.analysis.generate_candidate_score')
def generate_candidate_score(application_id):
    row = ensure(application_id)
    if row and row.status in ('QUEUED', 'PROCESSING'):
        calculate_score_revision.run(str(row.pk))
    return payload(Application.objects.get(pk=application_id))


@shared_task(ignore_result=True, acks_late=True, soft_time_limit=20, time_limit=30)
def calculate_score_revision(revision_id):
    ref = ScoreRevision.objects.select_related('evaluation').get(pk=revision_id)
    if not ref.evaluation_id or ref.status not in ('QUEUED', 'PROCESSING'):
        return
    token = uuid.uuid4()
    with transaction.atomic():
        app = lock_sources(ref)
        row = ScoreRevision.objects.select_for_update().get(pk=revision_id)
        if row.status not in ('QUEUED', 'PROCESSING') or row.next_attempt_at > timezone.now():
            return
        if row.lease_until and row.lease_until > timezone.now():
            return
        if digest(row.inputs) != row.key:
            row.status, row.error_code = 'FAILED', 'INPUT_HASH_MISMATCH'
            row.save()
            return
        if digest(current_inputs(app)[0]) != row.key:
            row.status, row.error_code = 'STALE', 'SOURCE_SUPERSEDED'
            row.save()
            return
        if row.attempts >= 4:
            row.status, row.error_code = 'FAILED', 'ATTEMPTS_EXHAUSTED'
            row.claim_token = row.lease_until = None
            row.save()
            return
        row.attempts += 1
        row.claim_token, row.lease_until = token, timezone.now() + timedelta(seconds=60)
        row.status = 'PROCESSING'
        row.save()
    try:
        result = combined_score(row.inputs['screening_match_percentage'], row.inputs['interview_performance_percentage'],
            weight_match=row.inputs['weights']['match'], weight_interview=row.inputs['weights']['interview'])
        with transaction.atomic():
            app = lock_sources(ref)
            current = ScoreRevision.objects.select_for_update().get(pk=revision_id)
            if current.claim_token != token or current.status != 'PROCESSING':
                return
            if digest(current_inputs(app)[0]) != current.key:
                current.status, current.error_code = 'STALE', 'SOURCE_SUPERSEDED'
                current.claim_token = current.lease_until = None
                current.save()
                return
            current.status, current.final_score = 'COMPLETE', result['final_score']
            current.completed_at = timezone.now()
            current.claim_token = current.lease_until = None
            current.save()
            CandidateScore.objects.update_or_create(application=app, defaults={'current_revision': current,
                'final_score': result['final_score'], 'breakdown': {'match': float(current.screening_match_percentage),
                'interview': float(current.interview_performance_percentage), 'behavioral': None},
                'explanation': 'Combined recommendation from screening and evaluated answers only.'})
            from ..notifications import queue_notification
            queue_notification(app.job.recruiter.user_id, 'score_ready', resource_type='score_revision',
                resource_id=current.pk, revision=current.number,
                links={'application_id': app.pk, 'interview_id': current.evaluation.interview_id},
                notification_id=current.pk, deliver_inapp=True)
            from ..audit import record_audit
            record_audit(actor=None, action='score.completed', target=current, changed_fields=['status'],
                revision=current.number, application_id=app.pk, interview_id=current.evaluation.interview_id)
    except Exception:
        ScoreRevision.objects.filter(pk=revision_id, claim_token=token, status='PROCESSING', completed_at__isnull=True).update(
            status='FAILED' if row.attempts >= 4 else 'QUEUED', error_code='SCORING_FAILED', claim_token=None,
            lease_until=None, next_attempt_at=timezone.now() + timedelta(seconds=15 * 2 ** row.attempts))


@shared_task(ignore_result=True)
def recover_final_scores():
    state = ScoringPolicyState.objects.filter(pk=1, rebuild_pending=True).first()
    if state:
        apps = Application.objects.order_by('pk')
        if state.rebuild_cursor:
            apps = apps.filter(pk__gt=state.rebuild_cursor)
        ids = list(apps.values_list('pk', flat=True)[:100])
        # Advance only after all intents commit. Crashes repeat an idempotent page.
        for pk in ids:
            ensure(pk)
        with transaction.atomic():
            current = ScoringPolicyState.objects.select_for_update().get(pk=1)
            if current.epoch == state.epoch and current.rebuild_cursor == state.rebuild_cursor:
                current.rebuild_cursor = ids[-1] if ids else current.rebuild_cursor
                current.rebuild_pending = len(ids) == 100
                current.save()
    due = ScoreRevision.objects.filter(status__in=('QUEUED', 'PROCESSING'), next_attempt_at__lte=timezone.now()).filter(
        Q(lease_until__isnull=True) | Q(lease_until__lte=timezone.now()))
    for pk in due.order_by('next_attempt_at').values_list('pk', flat=True)[:100]:
        dispatch(pk)
