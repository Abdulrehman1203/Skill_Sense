"""Locked, versioned human recovery commands. Original evidence is retained."""
import copy
from datetime import timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .answer_evaluation import digest, prepare_evaluation_locked, freeze_context_locked
from .audit import record_audit
from .models import (Application, FeedbackChange, FeedbackReport, Interview, InterviewEvaluation,
    Question, ScreeningEvent, TranscriptPreparation, TranscriptRevision, WorkflowAction)
from .screening import ScreeningConflict


def locked(interview_id, actor):
    interview = get_object_or_404(Interview.objects.select_for_update(of=('self',)).select_related('application__job__recruiter'),
        pk=interview_id, application__job__recruiter__user=actor)
    app = Application.objects.select_for_update().get(pk=interview.application_id)
    return interview, app


def begin(app, interview, actor, action, data):
    if actor.role != 'RECRUITER':
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied()
    previous = WorkflowAction.objects.filter(application=app, action=action, expected_version=data['expected_version']).first()
    import json
    from django.core.serializers.json import DjangoJSONEncoder
    fingerprint = digest(json.loads(json.dumps(data, cls=DjangoJSONEncoder)))
    if previous:
        if previous.actor_id == actor.pk and previous.interview_id == (interview.pk if interview else None) and previous.details['request_hash'] == fingerprint:
            return previous, fingerprint
        raise ScreeningConflict('This command version was already used with different inputs.')
    if app.version != data['expected_version']:
        raise ScreeningConflict()
    if not data['reason'].strip():
        raise ValidationError({'reason': 'Explain this deliberate workflow action.'})
    return None, fingerprint


def finish(app, interview, actor, action, data, fingerprint, **details):
    row = WorkflowAction.objects.create(application=app, interview=interview, actor=actor, action=action,
        expected_version=app.version, reason=data['reason'], details={'request_hash': fingerprint, **details})
    app.version += 1
    app.save(update_fields=['status', 'version', 'updated_at'])
    record_audit(actor=actor, action=action, target=row, changed_fields=['workflow'], revision=app.version,
        application_id=app.pk, interview_id=interview.pk if interview else None)
    return row


@transaction.atomic
def rerun(interview_id, actor, data):
    interview, app = locked(interview_id, actor)
    prior, key = begin(app, interview, actor, 'evaluation.rerun', data)
    if prior:
        return InterviewEvaluation.objects.get(pk=prior.details['evaluation_id'])
    latest = interview.evaluations.order_by('-revision').first()
    if not latest or latest.revision != data['expected_revision']:
        raise ScreeningConflict('Evaluation revision changed.')
    if latest.status in ('QUEUED', 'PROCESSING'):
        raise ScreeningConflict('Wait for the active evaluation before requesting another run.')
    if interview.evaluations.filter(requested_by__isnull=False, created_at__gte=timezone.now() - timedelta(minutes=10)).count() >= 4:
        from rest_framework.exceptions import Throttled
        raise Throttled(wait=600, detail='Too many manual evaluation reruns. Try again later.')
    prep = TranscriptPreparation.objects.filter(interview=interview, state='READY').first()
    if not prep or interview.status != 'DONE' or interview.completion_outcome != 'USABLE':
        raise ValidationError('Verified usable completion and selected ready transcript are required.')
    row = prepare_evaluation_locked(interview, prep, rerun=True, actor=actor, reason=data['reason'])
    finish(app, interview, actor, 'evaluation.rerun', data, key, evaluation_id=str(row.pk))
    return row


@transaction.atomic
def select_transcript(interview_id, actor, data):
    interview, app = locked(interview_id, actor)
    prior, key = begin(app, interview, actor, 'transcript.selected', data)
    if prior:
        return TranscriptPreparation.objects.get(interview=interview)
    prep = TranscriptPreparation.objects.select_for_update().filter(interview=interview).first()
    if not prep or prep.selection_version != data['expected_selection_version']:
        raise ScreeningConflict('Transcript selection changed.')
    if interview.status != 'DONE' or interview.completion_outcome in ('FAILED', 'LEGACY_UNVERIFIED'):
        raise ValidationError('Only a verified ended call can select corrected evidence.')
    revision = get_object_or_404(TranscriptRevision, pk=data['transcript_id'], interview=interview,
        call_id=interview.retell_session_id)
    if not revision.text.strip() or not revision.turns:
        raise ValidationError('Selected evidence must contain text and attributable turns.')
    prep.selected, prep.selected_source, prep.state = revision, 'recruiter_selected', 'READY'
    prep.selected_by, prep.selection_reason = actor, data['reason']
    prep.selection_version += 1
    prep.reason = ''
    prep.save()
    interview.session.transcript = revision.text
    interview.session.save(update_fields=['transcript'])
    if interview.completion_outcome == 'INSUFFICIENT_EVIDENCE':
        interview.completion_outcome = 'USABLE'
        interview.save(update_fields=['completion_outcome', 'updated_at'])
        if app.status not in ('DECISION', 'REJECTED') and app.interviews.order_by('-created_at', '-pk').first().pk == interview.pk:
            app.status = 'INTERVIEWED'
    prepare_evaluation_locked(interview, prep, rerun=True, actor=actor, reason=data['reason'])
    finish(app, interview, actor, 'transcript.selected', data, key, transcript_id=str(revision.pk))
    return prep


@transaction.atomic
def reschedule(interview_id, actor, data, *, reopen=False):
    interview, app = locked(interview_id, actor)
    action = 'interview.reopened' if reopen else 'interview.rescheduled'
    prior, key = begin(app, interview, actor, action, data)
    if prior:
        return Interview.objects.get(pk=prior.details['result_id'])
    if app.status in ('DECISION', 'REJECTED'):
        raise ScreeningConflict('Reverse the terminal decision deliberately before scheduling another attempt.')
    if data['scheduled_at'] <= timezone.now():
        raise ValidationError({'scheduled_at': 'Select a future time.'})
    if interview.application.interviews.order_by('-created_at', '-pk').first().pk != interview.pk:
        raise ScreeningConflict('Only the latest attempt can be changed.')
    if reopen:
        if interview.status != 'DONE' or not interview.completed_at:
            raise ValidationError('Only a completed attempt can be reopened as a new attempt.')
        if not interview.question_set_id or not interview.questions.filter(approved=True).exists():
            raise ValidationError('An approved frozen question bank is required.')
        result = Interview.objects.create(application=app, question_set=interview.question_set,
            supersedes=interview, scheduled_at=data['scheduled_at'], question_preparation_mode=interview.question_preparation_mode,
            question_fallback_reason=interview.question_fallback_reason)
        questions = list(interview.questions.filter(approved=True).order_by('position', 'id'))
        Question.objects.bulk_create([Question(interview=result, text=q.text, category=q.category, source=q.source,
            position=q.position, prepared_question=q.prepared_question, approved=True) for q in questions])
        source_context = freeze_context_locked(interview)
        context = copy.deepcopy(source_context.snapshot)
        if data.get('resume_remaining'):
            from .conversation_control import prepare_continuation
            continuation = prepare_continuation(interview, result)
            context['continuation_evidence_hash'] = continuation.carried_hash
        context['questions'] = [{'id': str(q.pk), 'text': q.text, 'category': q.category,
            'criteria': next(old['criteria'] for old in context['questions'] if old['text'] == q.text and old['category'] == q.category)}
            for q in result.questions.all()]
        from .models import EvaluationContext
        EvaluationContext.objects.create(interview=result, snapshot=context, content_hash=digest(context),
            contract_version=source_context.contract_version)
    else:
        session = getattr(interview, 'session', None)
        if interview.status != 'SCHEDULED' or interview.retell_session_id or (session and (session.started_at or session.creation_uncertain)):
            raise ScreeningConflict('A started or uncertain call cannot be rescheduled.')
        interview.scheduled_at = data['scheduled_at']
        interview.save(update_fields=['scheduled_at', 'updated_at'])
        result = interview
    previous_status = app.status
    app.status = 'SCHEDULED'
    finish(app, interview, actor, action, data, key, result_id=str(result.pk), from_status=previous_status)
    ScreeningEvent.objects.create(application=app, actor=actor, source='MANUAL', from_status=previous_status,
        to_status=app.status, reason=data['reason'], assessment_id=app.current_assessment_id)
    from .notifications import queue_notification
    queue_notification(app.candidate.user_id, 'interview_scheduled', resource_type='interview', resource_id=result.pk,
        revision=app.version, links={'application_id': app.pk})
    return result


@transaction.atomic
def reverse_decision(application_id, actor, data):
    # Same Interview -> Application order as decision writers.
    list(Interview.objects.select_for_update().filter(application_id=application_id).order_by('pk'))
    app = get_object_or_404(Application.objects.select_for_update(of=('self',)), pk=application_id, job__recruiter__user=actor)
    prior, key = begin(app, None, actor, 'decision.reversed', data)
    if prior:
        return app
    if app.status != 'DECISION':
        raise ScreeningConflict('There is no current final decision to reverse.')
    event = app.screening_events.filter(to_status='DECISION').order_by('-created_at', '-pk').first()
    if not event:
        raise ValidationError('Historical decision has no attributable prior stage; operator reconciliation is required.')
    restored = event.from_status
    if restored not in Application.Status.values or restored in ('DECISION', 'REJECTED'):
        raise ValidationError('Decision history does not define a safe stage to restore.')
    latest = app.interviews.order_by('-created_at', '-pk').first()
    if latest:
        if latest.status == 'DONE' and latest.completed_at:
            restored = 'INTERVIEWED' if latest.completion_outcome == 'USABLE' else 'INTERVIEW_FAILED'
        elif latest.status == 'LIVE':
            restored = 'IN_PROGRESS'
        elif latest.status == 'SCHEDULED':
            restored = 'SCHEDULED'
    app.status = restored
    finish(app, None, actor, 'decision.reversed', data, key, restored_stage=restored)
    ScreeningEvent.objects.create(application=app, source='MANUAL', actor=actor, from_status='DECISION',
        to_status=restored, reason=data['reason'], assessment_id=app.current_assessment_id)
    from .notifications import queue_notification
    queue_notification(app.candidate.user_id, 'application_status_changed', resource_type='application', resource_id=app.pk, revision=app.version)
    return app


@transaction.atomic
def change_feedback(report_id, actor, data, *, retract=False):
    ref = get_object_or_404(FeedbackReport, pk=report_id, evaluation__interview__application__job__recruiter__user=actor)
    interview, app = locked(ref.evaluation.interview_id, actor)
    row = FeedbackReport.objects.select_for_update().get(pk=report_id)
    action = 'feedback.retracted' if retract else 'feedback.edited'
    prior, key = begin(app, interview, actor, action, data)
    if prior:
        return row
    if row.edit_version != data['expected_edit_version']:
        raise ScreeningConflict('Feedback editing/retraction state changed.')
    if retract:
        if not row.released_at or row.retracted_at:
            raise ScreeningConflict('Only currently released feedback can be retracted.')
        row.retracted_at, row.retracted_by, row.retraction_reason = timezone.now(), actor, data['reason']
    else:
        if row.released_at or row.status != 'READY':
            raise ScreeningConflict('Only an unreleased READY report can be edited.')
        # Numeric evidence, strengths/gaps and dimensions cannot be rewritten.
        # Recruiter-supplied text is intentionally separate and explicitly public.
        row.amendments = data['amendments']
        row.amendments_hash = digest(row.amendments)
    row.edit_version += 1
    row.save()
    FeedbackChange.objects.create(report=row, version=row.edit_version, action='RETRACT' if retract else 'EDIT',
        actor=actor, reason=data['reason'], content=row.amendments if not retract else {})
    finish(app, interview, actor, action, data, key, report_id=str(row.pk))
    if retract:
        from .notifications import queue_notification
        queue_notification(app.candidate.user_id, 'feedback_retracted', resource_type='feedback_report', resource_id=row.pk,
            revision=row.edit_version, links={'interview_id': interview.pk, 'application_id': app.pk}, deliver_inapp=True)
    return row
