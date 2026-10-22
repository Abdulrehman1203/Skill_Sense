"""Versioned evidence, frozen category criteria and strict answer evaluation contract.

All writers lock Interview before transcript/evaluation rows. No network under locks.
"""
import hashlib
import json
import math
import re
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import (AnswerEvaluation, EvaluationContext, InterviewEvaluation,
                     TranscriptPreparation, TranscriptRevision)

CRITERIA = {
    'TECHNICAL': {'accuracy': 'Technical accuracy against the job requirements, acknowledging alternatives.',
                  'reasoning': 'Sound explanation, trade-offs and verification.'},
    'BEHAVIORAL': {'specificity': 'Concrete context, personal actions and observable outcomes.',
                   'reflection': 'Reflection and collaboration supported by the example; do not infer personality or protected traits.'},
    'SITUATIONAL': {'judgment': 'Plausible prioritization, risks and stakeholder considerations.',
                    'reasoning': 'Explained options, assumptions and trade-offs; multiple approaches may be valid.'},
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def normalized_turns(raw):
    # Never silently truncate/drop malformed speech and then infer an unanswered
    # question from the resulting artificial silence.
    if not isinstance(raw, list) or len(raw) > 2000 or any(
            not isinstance(item, dict) or item.get('role') not in ('agent', 'user')
            or not isinstance(item.get('content'), str) for item in raw):
        return []
    turns = []
    for item in raw:
        content = item.get('content')
        role = item.get('role')
        if role in ('agent', 'user') and isinstance(content, str) and content.strip():
            turn = {'id': f't{len(turns) + 1}', 'role': role, 'text': content.strip()}
            words = item.get('words')
            start = item.get('start', words[0].get('start') if isinstance(words, list) and words and isinstance(words[0], dict) else None)
            end = item.get('end', words[-1].get('end') if isinstance(words, list) and words and isinstance(words[-1], dict) else None)
            if (type(start) in (int, float) and type(end) in (int, float)
                    and math.isfinite(start) and math.isfinite(end) and 0 <= start <= end):
                turn.update(start_seconds=start, end_seconds=end)
            turns.append(turn)
    return turns


def freeze_context_locked(interview):
    existing = EvaluationContext.objects.filter(interview=interview).first()
    if existing:
        return existing
    job = interview.application.job
    # For prepared calls use the job captured during qualification, not a later edit.
    prepared = interview.question_set.input_snapshot if interview.question_set_id else {}
    job_snapshot = prepared.get('policy') or prepared.get('job') or {k: getattr(job, k) for k in ('title', 'description', 'requirements', 'skills_required')}
    questions = [{'id': str(q.pk), 'text': q.text, 'category': q.category,
                  'criteria': CRITERIA[q.category]} for q in interview.questions.filter(approved=True).order_by('position', 'created_at', 'pk')]
    snapshot = {'job': job_snapshot, 'questions': questions,
                'origin': 'PREPARED' if interview.question_set_id else 'LEGACY_AT_FIRST_USE'}
    return EvaluationContext.objects.create(interview=interview, snapshot=snapshot, content_hash=digest(snapshot))


def accept_transcript_locked(interview, session, payload, source):
    """Only authenticated webhook or authenticated provider-GET callers enter here.

    Select non-decreasing text/turn completeness, with analyzed > ended priority.
    Keep all distinct receipts, even when a receipt is not selected. No claim that
    length proves semantic quality: revisions remain reviewable by recruiters.
    """
    content = {'text': payload.get('transcript', ''), 'turns': normalized_turns(payload.get('transcript_object', []))}
    content_hash = digest(content)
    revision = TranscriptRevision.objects.filter(interview=interview, call_id=payload['retell_session_id'], content_hash=content_hash).first()
    if revision is None:
        revision = TranscriptRevision.objects.create(interview=interview,
            number=interview.transcript_revisions.count() + 1, call_id=payload['retell_session_id'],
            source=source, content_hash=content_hash, text=content['text'], turns=content['turns'])
    prep, _ = TranscriptPreparation.objects.get_or_create(interview=interview,
        defaults={'deadline': timezone.now() + timedelta(seconds=getattr(settings, 'TRANSCRIPT_ENRICHMENT_WAIT_SECONDS', 300))})
    previous = prep.selected
    rank = lambda name: 2 if name == 'call_analyzed' else 1
    quality = lambda row: (len(row.text.strip()), sum(len(t['text']) for t in row.turns), len(row.turns))
    acceptable = not prep.selected_by_id and (previous is None or (revision.number >= previous.number and rank(source) >= rank(prep.selected_source)
        and (all(a >= b for a, b in zip(quality(revision), quality(previous)))
             or (rank(source) > rank(prep.selected_source) and bool(revision.text.strip()) and bool(revision.turns)))))
    # Identical content from analyzed still upgrades readiness without duplicating evidence.
    if acceptable:
        if prep.selected_id != revision.pk or prep.selected_source != source:
            prep.selection_version += 1
        prep.selected, prep.selected_source = revision, source
        session.transcript = revision.text
        session.save(update_fields=['transcript'])
    if session.audio_terminal_status in ('error', 'not_connected'):
        prep.state, prep.reason = 'UNAVAILABLE', 'CALL_FAILED'
    elif prep.selected and (prep.selected.text.strip() or prep.selected.turns) and (prep.selected_source == 'call_analyzed' or prep.deadline <= timezone.now()):
        prep.state, prep.reason = 'READY', ''
    elif prep.deadline <= timezone.now():
        prep.state, prep.reason = 'UNAVAILABLE', 'TRANSCRIPT_EMPTY'
    prep.save()
    prepare_evaluation_locked(interview, prep)
    return revision


def prepare_evaluation_locked(interview, prep=None, *, rerun=False, actor=None, reason=''):
    prep = prep or TranscriptPreparation.objects.filter(interview=interview).first()
    if not prep or not prep.selected or interview.status != 'DONE':
        return None
    if prep.state == 'WAITING':
        return None
    context = freeze_context_locked(interview)
    existing = InterviewEvaluation.objects.filter(interview=interview, transcript=prep.selected, context=context,
        final_evidence=prep.selected_source in ('call_analyzed', 'recruiter_selected')).order_by('-run_number').first()
    if existing and not rerun:
        return existing
    latest = interview.evaluations.order_by('-revision').first()
    unavailable = prep.state == 'UNAVAILABLE' or not context.snapshot['questions']
    row = InterviewEvaluation.objects.create(interview=interview, transcript=prep.selected, context=context,
        revision=(latest.revision + 1 if latest else 1), supersedes=latest,
        model_name=getattr(settings, 'GEMINI_EVALUATION_MODEL_NAME', settings.GEMINI_MODEL_NAME),
        final_evidence=prep.selected_source in ('call_analyzed', 'recruiter_selected'),
        run_number=(existing.run_number + 1 if existing else 0), requested_by=actor, request_reason=reason,
        prompt_version='answers-v2',
        status='UNAVAILABLE' if unavailable else 'QUEUED',
        error_code=(prep.reason or 'NO_FROZEN_QUESTIONS') if unavailable else '')
    from .audit import record_audit
    from .models import FeedbackReport
    FeedbackReport.objects.create(evaluation=row, revision=row.revision)
    record_audit(actor=None, action='evaluation.superseding_requested' if latest else 'evaluation.requested',
        target=row, changed_fields=['status'], revision=row.revision,
        application_id=interview.application_id, interview_id=interview.pk)
    interview.evaluation_status = 'UNAVAILABLE' if unavailable else 'PENDING'
    # Late enrichment repairs insufficient evidence, never a verified failed call.
    if prep.state == 'READY' and interview.completion_outcome == 'INSUFFICIENT_EVIDENCE':
        interview.completion_outcome = 'USABLE'
        from .interview_workflow import stage_locked
        from .models import Application
        stage_locked(interview, Application.Status.INTERVIEWED, 'Authenticated transcript enrichment became usable.')
    interview.save(update_fields=['evaluation_status', 'completion_outcome', 'updated_at'])
    if not unavailable:
        transaction.on_commit(lambda: dispatch_evaluation(row.pk))
    else:
        from .evaluation_lifecycle import terminal_locked
        terminal_locked(interview, row)
    return row


def dispatch_evaluation(pk):
    from .tasks.evaluation import evaluate_interview
    try:
        evaluate_interview.apply_async(args=[str(pk)], retry=False)
    except Exception:
        import logging
        logging.getLogger(__name__).warning('Evaluation dispatch deferred evaluation_id=%s', pk)


def evaluation_input(row):
    if digest(row.context.snapshot) != row.context.content_hash or digest({'text': row.transcript.text, 'turns': row.transcript.turns}) != row.transcript.content_hash:
        raise InvalidEvaluation('EVIDENCE_HASH_MISMATCH')
    result = {'contract_version': row.prompt_version, 'context': row.context.snapshot, 'final_evidence': row.final_evidence,
            'transcript': {'hash': row.transcript.content_hash, 'text': row.transcript.text, 'turns': row.transcript.turns}}
    from .models import InterviewConversation
    conversation = InterviewConversation.objects.filter(interview_id=row.interview_id).first()
    if conversation and conversation.cursor < len(conversation.questions):
        # A terminated call is not proof the candidate was allowed to finish the
        # current answer. Keep the unresolved final segment partial/unavailable.
        result['final_evidence'] = False
    if conversation and conversation.carried_evidence:
        evidence = conversation.carried_evidence
        if (digest(evidence) != conversation.carried_hash
                or row.context.snapshot.get('continuation_evidence_hash') != conversation.carried_hash):
            raise InvalidEvaluation('CONTINUATION_HASH_MISMATCH')
        result['transcript'] = {'hash': digest({'current': row.transcript.content_hash, 'carried': conversation.carried_hash}),
            'text': '[Previous authenticated answer segments]\n' + '\n'.join(t['text'] for t in evidence['turns'])
                + '\n[Current authenticated call]\n' + row.transcript.text,
            'turns': evidence['turns'] + row.transcript.turns,
            'continuation_source': {k: evidence[k] for k in ('source_interview', 'source_transcript', 'source_hash')}}
    if row.prompt_version == 'answers-v2':
        from .answer_extraction import extract
        saved = getattr(row, 'extraction', None)
        if saved and (saved.input_hash != digest(result) or saved.content_hash != digest(saved.content)):
            raise InvalidEvaluation('EXTRACTION_HASH_MISMATCH')
        result['alignment'] = saved.content if saved else extract(result)
    return result


class InvalidEvaluation(ValueError):
    pass


def validate_evaluation(data, inputs):
    """No provider overall score is trusted. Citation quotes must be literal evidence.

    Exact frozen question wording anchors asked turns. Unalignable/paraphrased
    prompts produce missing evidence, never an invented unanswered zero.
    """
    def require(ok):
        if not ok:
            raise InvalidEvaluation('INVALID_EVALUATION_OUTPUT')
    def number(value):
        return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
    def prose(value):
        return isinstance(value, str) and bool(value.strip()) and len(value) <= 5000
    def texts(value):
        return isinstance(value, list) and len(value) <= 20 and all(prose(x) for x in value)
    clean = lambda text: re.sub(r'\W+', ' ', text.lower()).strip()
    questions = {q['id']: q for q in inputs['context']['questions']}
    turns = {t['id']: t for t in inputs['transcript']['turns']}
    ordered = list(turns)
    require(isinstance(data, dict) and set(data) == {'answers'} and isinstance(data['answers'], list))
    require(len(data['answers']) == len(questions))
    results, seen, scores = [], set(), []
    for answer in data['answers']:
        require(isinstance(answer, dict) and set(answer) == {'question_id', 'status', 'criteria', 'strengths', 'gaps', 'rationale', 'evidence', 'uncertainty'})
        qid = answer['question_id']
        require(isinstance(qid, str) and qid in questions and qid not in seen)
        seen.add(qid)
        q = questions[qid]
        status = answer['status']
        require(status in ('ANSWERED', 'NO_ANSWER', 'MISSING_EVIDENCE'))
        require(texts(answer['strengths']) and texts(answer['gaps']) and prose(answer['rationale']) and number(answer['uncertainty']))
        require(isinstance(answer['evidence'], list) and len(answer['evidence']) <= 100)
        refs = []
        for ref in answer['evidence']:
            require(isinstance(ref, dict) and set(ref) == {'turn_id', 'quote'})
            require(isinstance(ref['turn_id'], str) and ref['turn_id'] in turns and prose(ref['quote']))
            require(ref['quote'] in turns[ref['turn_id']]['text'])
            refs.append(turns[ref['turn_id']])
        alignment = next((a for a in inputs.get('alignment', {}).get('questions', []) if a['question_id'] == qid), None)
        asked = [t for t in refs if t['role'] == 'agent' and (
            (alignment and alignment['state'] in ('ANSWERED', 'NO_ANSWER') and t['id'] == alignment['asked_turn_id'])
            or (not alignment and clean(q['text']) in clean(t['text'])))]
        user_refs = [t for t in refs if t['role'] == 'user']
        criteria = answer['criteria']
        require(isinstance(criteria, dict) and set(criteria) == set(q['criteria']))
        if status == 'MISSING_EVIDENCE':
            require(all(v is None for v in criteria.values()))
        else:
            require(bool(asked) and all(number(v) for v in criteria.values()))
            start = ordered.index(asked[0]['id'])
            # Bound the answer at the next known frozen question, not a follow-up.
            end = next((i for i in range(start + 1, len(ordered)) if turns[ordered[i]]['role'] == 'agent'
                        and any(clean(other['text']) in clean(turns[ordered[i]]['text']) for other in questions.values())), len(ordered))
            segment_users = ([turns[tid] for tid in alignment['answer_turn_ids']] if alignment else
                [turns[ordered[i]] for i in range(start + 1, end) if turns[ordered[i]]['role'] == 'user'])
            if status == 'NO_ANSWER':
                require(not segment_users and not user_refs and all(v == 0 for v in criteria.values()))
                # Must have a later asked question or analyzed final evidence; a truncated text is not proof of silence.
                require(alignment['state'] == 'NO_ANSWER' if alignment else end < len(ordered) or inputs.get('final_evidence', False))
            else:
                require(bool(user_refs) and all(t in segment_users for t in user_refs))
            scores.append(sum(criteria.values()) / len(criteria))
        results.append(answer)
    coverage = len(scores) / len(questions) if questions else 0
    return results, (100 * sum(scores) / len(scores) if scores else None), coverage
