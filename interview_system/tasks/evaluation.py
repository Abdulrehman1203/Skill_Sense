"""Durable leased evaluation and bounded transcript enrichment recovery."""
import logging
import uuid
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..answer_evaluation import (InvalidEvaluation, dispatch_evaluation, evaluation_input, fallback_evaluation_locked,
                                prepare_evaluation_locked, uses_grading_fallback, validate_evaluation)
from ..integrations.gemini_client import EvaluationProviderError, GradingBatchPending, has_grading_backup_key
from ..models import AnswerEvaluation, Application, Interview, InterviewEvaluation, TranscriptPreparation
from .guards import skip_missing_resource

logger = logging.getLogger(__name__)


class EvaluationClaimLost(Exception):
    pass


def checkpoint_answer(evaluation_id, token, question_id, result):
    with transaction.atomic():
        row = InterviewEvaluation.objects.select_for_update().get(pk=evaluation_id)
        if row.claim_token != token or row.status != 'PROCESSING' or row.lease_until <= timezone.now():
            raise EvaluationClaimLost
        answers = {**row.grading_progress.get('answers', row.grading_progress), question_id: result}
        row.grading_progress = {'answers': answers, 'last_progress_attempt': row.attempts}
        row.save(update_fields=['grading_progress'])


def finalize_locked(interview, row, *, replaced_by_fallback=False):
    from ..evaluation_lifecycle import terminal_locked
    from ..final_scoring import ensure_locked

    if replaced_by_fallback or interview.evaluations.filter(revision__gt=row.revision).exists():
        terminal_locked(interview, row, notify=not replaced_by_fallback)
        return
    interview.evaluation_status = 'READY' if row.status == 'READY' and row.coverage == 1 else (
        'FAILED' if row.status == 'FAILED' else 'UNAVAILABLE')
    interview.save(update_fields=['evaluation_status', 'updated_at'])
    terminal_locked(interview, row)
    ensure_locked(Application.objects.select_for_update().get(pk=interview.application_id))


@shared_task(ignore_result=True, acks_late=True, soft_time_limit=65, time_limit=75)
@skip_missing_resource(InterviewEvaluation, Interview)
def evaluate_interview(evaluation_id):
    token = uuid.uuid4()
    interview_id = InterviewEvaluation.objects.values_list('interview_id', flat=True).get(pk=evaluation_id)
    with transaction.atomic():
        interview = Interview.objects.select_for_update().get(pk=interview_id)
        row = InterviewEvaluation.objects.select_for_update(of=('self',)).select_related('context', 'transcript').get(pk=evaluation_id)
        if row.status not in ('QUEUED', 'PROCESSING') or row.next_attempt_at > timezone.now():
            return
        if row.lease_until and row.lease_until > timezone.now():
            return
        if row.attempts - row.grading_progress.get('last_progress_attempt', 0) >= 4:
            # A hard-killed final attempt must also have a terminal state.
            row.status, row.error_code = 'FAILED', 'ATTEMPTS_EXHAUSTED'
            row.claim_token = row.lease_until = None
            row.save()
            finalize_locked(interview, row)
            return
        row.claim_token, row.lease_until = token, timezone.now() + timedelta(seconds=120)
        row.status, row.attempts = 'PROCESSING', row.attempts + 1
        row.save()
        from ..audit import record_audit
        record_audit(actor=None, action='evaluation.attempt_started', target=row,
            changed_fields=['attempts'], revision=row.attempts, application_id=interview.application_id, interview_id=interview.pk)
    try:
        from ..integrations.gemini_client import evaluate_answers
        inputs = evaluation_input(row)
        if row.prompt_version == 'answers-v2':
            from ..answer_evaluation import digest
            from ..models import AnswerExtraction
            # Preserve validated attribution even if the provider later fails.
            with transaction.atomic():
                current = InterviewEvaluation.objects.select_for_update().get(pk=row.pk)
                if current.claim_token != token or current.status != 'PROCESSING':
                    return
                AnswerExtraction.objects.get_or_create(evaluation=current, defaults={
                    'input_hash': digest({k: v for k, v in inputs.items() if k != 'alignment'}),
                    'content_hash': digest(inputs['alignment']), 'content': inputs['alignment']})
        credentials = {'use_fallback_key': True} if uses_grading_fallback(row) and has_grading_backup_key() else {}
        output = evaluate_answers(inputs, model_name=row.model_name,
            completed=row.grading_progress.get('answers', row.grading_progress),
            checkpoint=lambda question_id, result: checkpoint_answer(row.pk, token, question_id, result),
            **credentials)
        extraction = None
        if row.prompt_version == 'answers-v2':
            from ..answer_extraction import extract
            from ..answer_evaluation import digest
            from ..models import AnswerExtraction
            if isinstance(output, dict) and 'alignment' in output:
                output = dict(output)
                proposed = extract(inputs, output.pop('alignment'))
                if proposed != inputs['alignment']:
                    raise InvalidEvaluation('INVALID_ALIGNMENT')
                extraction = inputs['alignment']
            else:
                extraction = inputs['alignment']
            inputs['alignment'] = extraction
        answers, score, coverage = validate_evaluation(output, inputs)
        with transaction.atomic():
            interview = Interview.objects.select_for_update().get(pk=row.interview_id)
            current = InterviewEvaluation.objects.select_for_update().get(pk=row.pk)
            if current.claim_token != token or current.status != 'PROCESSING':
                return
            if extraction is not None:
                AnswerExtraction.objects.get_or_create(evaluation=current, defaults={
                    'input_hash': digest({k: v for k, v in inputs.items() if k != 'alignment'}),
                    'content_hash': digest(extraction), 'content': extraction})
                from ..models import Response, InterviewSession
                session = InterviewSession.objects.filter(interview=interview).first()
                if session:
                    turns = {t['id']: t['text'] for t in inputs['transcript']['turns']}
                    for item in extraction['questions']:
                        Response.objects.create(evaluation=current, session=session, question_id=item['question_id'],
                            answer_text='\n'.join(turns[tid] for tid in item['answer_turn_ids']))
            for answer in answers:
                AnswerEvaluation.objects.create(evaluation=current, question_id=answer['question_id'], result=answer)
            current.score, current.coverage = score, coverage
            current.status = 'READY' if score is not None else 'UNAVAILABLE'
            current.error_code = '' if score is not None else 'MISSING_ANSWER_EVIDENCE'
            current.published_at = timezone.now()
            current.claim_token = current.lease_until = None
            current.grading_progress = {}
            current.save()
            finalize_locked(interview, current)
    except EvaluationClaimLost:
        return
    except Exception as exc:
        # Never persist provider exception text, prompts or candidate content in error logs.
        with transaction.atomic():
            interview = Interview.objects.select_for_update().get(pk=row.interview_id)
            current = InterviewEvaluation.objects.select_for_update().get(pk=row.pk)
            if current.claim_token != token:
                return
            # Persist only closed-vocabulary diagnostics, never provider text.
            codes = {'INVALID_EVALUATION_OUTPUT', 'INVALID_EVIDENCE_QUOTE',
                'INVALID_CRITERION_IDS', 'INVALID_QUESTION_ATTRIBUTION',
                'INVALID_CRITERION_SCORE', 'INVALID_ANSWER_ATTRIBUTION',
                'INVALID_ALIGNMENT', 'INVALID_ALIGNMENT_REFERENCE',
                'EVIDENCE_HASH_MISMATCH', 'CONTINUATION_HASH_MISMATCH',
                'EXTRACTION_HASH_MISMATCH', 'INVALID_EVALUATION_JSON', 'EVALUATION_INPUT_TOO_LARGE',
                'EVALUATION_PROVIDER_EMPTY_RESPONSE'}
            failed_attempts = current.attempts - current.grading_progress.get('last_progress_attempt', 0)
            terminal = failed_attempts >= 4
            replaced_by_fallback = False
            if isinstance(exc, GradingBatchPending):
                current.error_code = ''
            elif isinstance(exc, EvaluationProviderError):
                current.error_code = exc.code
                terminal = terminal or not exc.transient
                if exc.transient:
                    current.provider_failures += 1
                    from ..integrations.groq_client import configured as groq_configured
                    if terminal or ((has_grading_backup_key() or groq_configured()) and current.provider_failures >= 2):
                        replaced_by_fallback = fallback_evaluation_locked(interview, current) is not None
                        terminal = terminal or replaced_by_fallback
            elif isinstance(exc, InvalidEvaluation):
                current.error_code = str(exc) if str(exc) in codes else 'INVALID_OUTPUT'
                terminal = terminal or current.error_code in {
                    'EVIDENCE_HASH_MISMATCH', 'CONTINUATION_HASH_MISMATCH',
                    'EXTRACTION_HASH_MISMATCH', 'EVALUATION_INPUT_TOO_LARGE'}
            elif isinstance(exc, TimeoutError):
                current.error_code = 'PROVIDER_UNAVAILABLE'
            else:
                current.error_code = 'EVALUATION_INTERNAL_ERROR'
                logger.error('Evaluation failed evaluation_id=%s type=%s', row.pk, type(exc).__name__)
            current.status = 'FAILED' if terminal else 'QUEUED'
            current.claim_token = current.lease_until = None
            delay = 0 if isinstance(exc, GradingBatchPending) else 15 * 2 ** min(4, max(1, failed_attempts))
            current.next_attempt_at = timezone.now() + timedelta(seconds=delay)
            current.save()
            if terminal:
                finalize_locked(interview, current, replaced_by_fallback=replaced_by_fallback)


@shared_task(ignore_result=True)
def recover_evaluations():
    now = timezone.now()
    pending = TranscriptPreparation.objects.filter(state='WAITING', next_sync_at__lte=now).order_by('next_sync_at')
    for interview_id in list(pending.values_list('interview_id', flat=True)[:50]):
        with transaction.atomic():
            interview = Interview.objects.select_for_update().get(pk=interview_id)
            prep = TranscriptPreparation.objects.select_for_update().get(interview=interview)
            if prep.state != 'WAITING' or prep.next_sync_at > now:
                continue
            if prep.deadline <= now:
                usable = prep.selected and (prep.selected.text.strip() or prep.selected.turns)
                prep.state, prep.reason = ('READY', '') if usable else ('UNAVAILABLE', 'TRANSCRIPT_EMPTY')
                prep.save()
                prepare_evaluation_locked(interview, prep)
                continue
            sync = prep.sync_attempts < 3
            prep.sync_attempts += int(sync)
            prep.next_sync_at = min(prep.deadline, now + timedelta(seconds=60)) if sync else prep.deadline
            prep.save()
            call_id = interview.retell_session_id
        if sync and call_id:
            try:
                from ..integrations import retell_client
                from ..voice import accept_event, normalize_call
                data = retell_client.get_call(call_id)
                if data.get('call_status') in ('ended', 'error', 'not_connected'):
                    accept_event(normalize_call(data), 'call_analyzed' if data.get('call_analysis') else 'provider_sync')
            except Exception:
                pass  # Bounded retries persisted before the network call.
    due = InterviewEvaluation.objects.filter(status__in=('QUEUED', 'PROCESSING'), next_attempt_at__lte=now).filter(
        Q(lease_until__isnull=True) | Q(lease_until__lte=now))
    for pk in due.order_by('next_attempt_at').values_list('pk', flat=True)[:100]:
        dispatch_evaluation(pk)
