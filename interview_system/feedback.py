"""Frozen feedback preparation, explicit recruiter release and safe candidate reads."""
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from .answer_evaluation import digest, evaluation_input, validate_evaluation
from .audit import record_audit
from .feedback_projection_v1 import project
from .models import EvaluationRelease, FeedbackReport, Interview, InterviewEvaluation
from .screening import ScreeningConflict


def prepare_locked(evaluation):
    """Caller holds Interview. Pure bounded projection; no provider or broker dependency."""
    row, _ = FeedbackReport.objects.get_or_create(evaluation=evaluation, defaults={'revision': evaluation.revision})
    if row.prepared_at or evaluation.status in ('QUEUED', 'PROCESSING'):
        return row
    row.prepared_at = timezone.now()
    try:
        if evaluation.status != 'READY' or evaluation.score is None or evaluation.published_at is None:
            row.status = 'FAILED' if evaluation.status == 'FAILED' else 'UNAVAILABLE'
            row.error_code = 'EVALUATION_UNAVAILABLE'
        else:
            answers = list(evaluation.answers.order_by('pk').values_list('result', flat=True))
            clean, score, coverage = validate_evaluation({'answers': answers}, evaluation_input(evaluation))
            if score is None or abs(score - evaluation.score) > 1e-8 or coverage != evaluation.coverage:
                raise ValueError('EVALUATION_RESULT_MISMATCH')
            row.content = project(evaluation.context.snapshot, clean, score, coverage)
            row.content_hash = digest(row.content)
            row.status = 'READY'
    except Exception:
        row.status, row.error_code, row.content, row.content_hash = 'FAILED', 'PROJECTION_VALIDATION_FAILED', {}, ''
    if row.status == 'READY':
        # Preserve prior deliberate G10 releases, but do not send new historic alerts.
        legacy = EvaluationRelease.objects.filter(evaluation=evaluation).first()
        if legacy:
            row.released_at, row.released_by_id = legacy.created_at, legacy.actor_id
    row.save()
    record_audit(actor=None, action='feedback.projection_prepared' if row.status == 'READY' else 'feedback.projection_failed',
        target=row, changed_fields=['status'], revision=row.revision,
        application_id=evaluation.interview.application_id, interview_id=evaluation.interview_id)
    return row


@transaction.atomic
def release(report_id, *, actor, expected_revision, expected_edit_version=None):
    ref = FeedbackReport.objects.select_related('evaluation').get(pk=report_id)
    interview = Interview.objects.select_for_update(of=('self',)).select_related('application__job__recruiter').get(pk=ref.evaluation.interview_id)
    if actor.role != 'RECRUITER' or interview.application.job.recruiter.user_id != actor.pk:
        raise PermissionDenied('Only the owning recruiter can release feedback.')
    row = FeedbackReport.objects.select_for_update(of=('self',)).select_related('evaluation').get(pk=report_id)
    if expected_revision != row.revision:
        raise ScreeningConflict('Feedback revision changed. Refresh before release.')
    if row.retracted_at:
        raise ScreeningConflict('Retracted feedback cannot be released again. Prepare a new evaluation/report revision.')
    if expected_edit_version != row.edit_version and (expected_edit_version is not None or row.edit_version != 1):
        raise ScreeningConflict('Feedback public guidance changed. Preview and confirm its edit version.')
    if row.released_at:
        return row
    if interview.status != 'DONE' or not interview.completed_at or interview.completion_outcome != 'USABLE' or row.status != 'READY':
        raise ValidationError('Only a verified completed interview with a prepared feedback report can be released.')
    if interview.evaluations.filter(revision__gt=row.evaluation.revision).exists():
        raise ScreeningConflict('New evidence exists. Review its feedback projection before release.')
    if digest(row.content) != row.content_hash:
        raise ValidationError('Feedback integrity check failed.')
    if row.amendments and digest(row.amendments) != row.amendments_hash:
        raise ValidationError('Public guidance integrity check failed.')
    row.released_at, row.released_by = timezone.now(), actor
    row.save(update_fields=['released_at', 'released_by'])
    EvaluationRelease.objects.get_or_create(evaluation=row.evaluation, defaults={'actor': actor})
    record_audit(actor=actor, action='interview.feedback_released', target=row,
        changed_fields=['released_at', 'released_by'], revision=row.revision,
        application_id=interview.application_id, interview_id=interview.pk)
    from .notifications import queue_notification
    queue_notification(interview.application.candidate.user_id, 'feedback_released', resource_type='feedback_report',
        resource_id=row.pk, revision=row.revision, links={'application_id': interview.application_id, 'interview_id': interview.pk}, deliver_inapp=True)
    return row


def candidate_state(interview, selected_report=None):
    released = selected_report or FeedbackReport.objects.filter(evaluation__interview=interview,
        released_at__isnull=False, status='READY').order_by('-revision').first()
    latest_released = FeedbackReport.objects.filter(evaluation__interview=interview, released_at__isnull=False).order_by('-revision').first()
    if (released and released.retracted_at) or (not selected_report and latest_released and latest_released.retracted_at):
        return {'state': 'RETRACTED', 'interview': str(interview.pk), 'application': str(interview.application_id), 'report': None}
    if (released and released.released_at and released.status == 'READY' and digest(released.content) == released.content_hash
            and (not released.amendments or digest(released.amendments) == released.amendments_hash)):
        return {'state': 'RELEASED', 'interview': str(interview.pk), 'application': str(interview.application_id),
            'report': {'id': str(released.pk), 'revision': released.revision, 'contract_version': released.contract_version,
                         'released_at': released.released_at, **released.content,
                         **({'recruiter_public_guidance': released.amendments} if released.amendments else {})}}
    if released and released.released_at:
        return {'state': 'FAILED', 'interview': str(interview.pk), 'application': str(interview.application_id), 'report': None}
    latest = selected_report or FeedbackReport.objects.filter(evaluation__interview=interview).order_by('-revision').first()
    state = {'READY': 'UNRELEASED', 'FAILED': 'FAILED', 'UNAVAILABLE': 'UNAVAILABLE'}.get(latest.status, 'PENDING') if latest else 'PENDING'
    return {'state': state, 'interview': str(interview.pk), 'application': str(interview.application_id), 'report': None}
