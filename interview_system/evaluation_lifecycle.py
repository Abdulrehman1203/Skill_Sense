"""Atomic supporting records for a terminal evaluation; callers hold Interview."""
from .audit import record_audit
from .feedback import prepare_locked
from .notifications import queue_notification


def terminal_locked(interview, evaluation):
    if evaluation.status not in ('READY', 'FAILED', 'UNAVAILABLE'):
        return
    ready = evaluation.status == 'READY'
    prepare_locked(evaluation)
    record_audit(actor=None, action='evaluation.ready' if ready else 'evaluation.failed', target=evaluation,
        changed_fields=['status'], revision=evaluation.revision, application_id=interview.application_id, interview_id=interview.pk)
    queue_notification(interview.application.job.recruiter.user_id, 'evaluation_ready' if ready else 'evaluation_failed',
        resource_type='evaluation', resource_id=evaluation.pk, revision=evaluation.revision,
        links={'application_id': interview.application_id, 'interview_id': interview.pk}, deliver_inapp=True)
