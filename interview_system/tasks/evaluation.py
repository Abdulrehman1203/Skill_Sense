"""Durable leased evaluation and bounded transcript enrichment recovery."""
import uuid
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..answer_evaluation import (InvalidEvaluation, dispatch_evaluation, evaluation_input,
                                prepare_evaluation_locked, validate_evaluation)
from ..models import AnswerEvaluation, Interview, InterviewEvaluation, TranscriptPreparation


@shared_task(ignore_result=True, acks_late=True, soft_time_limit=65, time_limit=75)
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
        if row.attempts >= 4:
            # A hard-killed final attempt must also have a terminal state.
            row.status, row.error_code = 'FAILED', 'ATTEMPTS_EXHAUSTED'
            row.claim_token = row.lease_until = None
            row.save()
            from ..evaluation_lifecycle import terminal_locked
            terminal_locked(interview, row)
            if not interview.evaluations.filter(revision__gt=row.revision).exists():
                interview.evaluation_status = 'FAILED'
                interview.save(update_fields=['evaluation_status', 'updated_at'])
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
        output = evaluate_answers(inputs, model_name=row.model_name)
        extraction = None
        if row.prompt_version == 'answers-v2':
            from ..answer_extraction import extract
            from ..answer_evaluation import digest
            from ..models import AnswerExtraction
            if isinstance(output, dict) and 'alignment' in output:
                output = dict(output)
                extraction = extract(inputs, output.pop('alignment'))
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
                AnswerExtraction.objects.create(evaluation=current,
                    input_hash=digest({k: v for k, v in inputs.items() if k != 'alignment'}),
                    content_hash=digest(extraction), content=extraction)
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
            current.save()
            from ..evaluation_lifecycle import terminal_locked
            terminal_locked(interview, current)
            if not interview.evaluations.filter(revision__gt=current.revision).exists():
                interview.evaluation_status = 'READY' if coverage == 1 else 'UNAVAILABLE'
                interview.save(update_fields=['evaluation_status', 'updated_at'])
                from ..final_scoring import ensure_locked
                from ..models import Application
                app = Application.objects.select_for_update().get(pk=interview.application_id)
                ensure_locked(app)
    except Exception as exc:
        # Never persist provider exception text, prompts or candidate content in error logs.
        with transaction.atomic():
            interview = Interview.objects.select_for_update().get(pk=row.interview_id)
            current = InterviewEvaluation.objects.select_for_update().get(pk=row.pk)
            if current.claim_token != token:
                return
            current.status = 'FAILED' if current.attempts >= 4 else 'QUEUED'
            current.error_code = 'INVALID_OUTPUT' if isinstance(exc, InvalidEvaluation) else 'PROVIDER_UNAVAILABLE'
            current.claim_token = current.lease_until = None
            current.next_attempt_at = timezone.now() + timedelta(seconds=15 * 2 ** current.attempts)
            current.save()
            if current.status == 'FAILED' and not interview.evaluations.filter(revision__gt=current.revision).exists():
                interview.evaluation_status = 'FAILED'
                interview.save(update_fields=['evaluation_status', 'updated_at'])
            if current.status == 'FAILED':
                from ..evaluation_lifecycle import terminal_locked
                terminal_locked(interview, current)


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
