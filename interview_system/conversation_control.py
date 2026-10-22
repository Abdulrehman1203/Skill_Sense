"""Authenticated, evidence-checked dialogue progression. No grading by the agent.

Lock order: Interview -> InterviewConversation. A built-in end_call tool must NOT
be installed on the configured agent; the durable stop job owns normal hang-up.
"""
import hashlib
import json
import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import ConversationReceipt, Interview, InterviewConversation


def clean(text):
    return re.sub(r'\W+', ' ', str(text).casefold()).strip()


def initialize_locked(interview):
    questions = list(interview.questions.filter(approved=True).order_by('position', 'id').values('id', 'text', 'category'))
    if not questions or len(questions) > 100 or not interview.retell_session_id:
        raise ValidationError('A frozen approved question bank and provider call are required.')
    for question in questions:
        question['id'] = str(question['id'])
    row, _ = InterviewConversation.objects.get_or_create(interview=interview, defaults={
        'call_id': interview.retell_session_id, 'questions': questions,
        'progress': {q['id']: {'status': 'NOT_ASKED'} for q in questions}})
    if row.call_id.startswith('pending:'):
        row.call_id = interview.retell_session_id
        row.save(update_fields=['call_id'])
    if row.call_id != interview.retell_session_id:
        raise ValidationError('Conversation call identity changed.')
    return row


def snapshot(interview):
    row = InterviewConversation.objects.filter(interview=interview).first()
    if not row:
        return {'outcome': 'UNVERIFIED', 'answered': None, 'skipped': None, 'total': None, 'paused': False}
    terminal = interview.status == 'DONE' or bool(getattr(getattr(interview, 'session', None), 'audio_ended_at', None))
    outcome = ('COMPLETED' if row.end_kind == 'COMPLETED' else 'ENDED_EARLY' if row.end_kind == 'CANDIDATE_ENDED'
               else 'INTERRUPTED') if terminal else 'ENDING' if row.stop_requested_at else 'PAUSED' if row.paused else 'IN_PROGRESS'
    return {'outcome': outcome, 'answered': sum(p['status'] == 'ANSWER_CAPTURED' for p in row.progress.values()),
            'skipped': sum(p['status'] == 'EXPLICITLY_SKIPPED' for p in row.progress.values()),
            'total': len(row.questions), 'paused': row.paused}


def reply(row, message=''):
    question = row.questions[row.cursor] if row.cursor < len(row.questions) else None
    return {'revision': row.revision, 'current_question': question,
            'state': 'ENDING' if row.stop_requested_at else 'PAUSED' if row.paused else 'READY_TO_FINISH' if question is None else 'AWAITING_ANSWER',
            'instruction': message or ('Wait quietly until the candidate explicitly asks to resume.' if row.paused else 'Ask this question verbatim and wait for the candidate.' if question else
                                     'All questions are resolved. Ask whether the candidate has finished; wait for an explicit confirmation before finish.')}


def candidate_segment(row, call):
    """Require a literal asked anchor in the signed provider transcript.

    Repetition uses the latest anchor, so asking for repetition never becomes an
    answer. Do not accept words spoken before this question or after another one.
    """
    turns = call.get('transcript_object')
    if not isinstance(turns, list) or len(turns) > 10000:
        return []
    if row.cursor >= len(row.questions):
        return []
    wording = clean(row.questions[row.cursor]['text'])
    anchors = [i for i, t in enumerate(turns) if isinstance(t, dict) and t.get('role') == 'agent'
               and wording and wording in clean(t.get('content', ''))]
    if not anchors:
        return []
    segment = turns[anchors[-1] + 1:]
    other_questions = [clean(q['text']) for q in row.questions if q['id'] != row.questions[row.cursor]['id']]
    users = []
    for index, turn in enumerate(segment, anchors[-1] + 1):
        if not isinstance(turn, dict):
            continue
        text = turn.get('content', '')
        if not isinstance(text, str):
            continue
        if turn.get('role') == 'agent' and any(q and q in clean(text) for q in other_questions):
            break
        if turn.get('role') == 'user' and clean(text):
            users.append((index, text))
    return users


def is_clarification(text):
    value = clean(text)
    return (not value or any(word in value for word in (
        'unintelligible', 'inaudible', 'background noise', 'can you repeat', 'could you repeat',
        'please repeat', 'repeat it', 'repeat that', 'say that again', 'cannot hear', 'can t hear',
        'didn t hear', 'did not hear', 'one moment', 'hold on', 'need a moment', 'give me a moment',
        'please wait', 'please pause')) or value in ('yes', 'no', 'okay', 'ok', 'sorry', 'pardon', 'repeat'))


def last_user(call):
    turns = call.get('transcript_object') or []
    if not isinstance(turns, list):
        return ''
    return next((t.get('content', '') for t in reversed(turns) if isinstance(t, dict) and t.get('role') == 'user'), '')


@transaction.atomic
def control(payload):
    call, args = payload.get('call'), payload.get('args')
    if payload.get('name') != 'interview_control' or not isinstance(call, dict) or not isinstance(args, dict):
        raise ValidationError('Invalid conversation command.')
    if not isinstance(call.get('call_id'), str) or not 1 <= len(call['call_id']) <= 255:
        raise ValidationError('Invalid provider call identity.')
    if set(args) - {'action', 'question_id', 'evidence'}:
        raise ValidationError('Unsupported conversation arguments.')
    interview = Interview.objects.select_for_update().filter(retell_session_id=call.get('call_id')).first()
    if interview is None or interview.status != 'LIVE':
        raise ValidationError('Call is not an active interview.')
    initialize_locked(interview)
    row = InterviewConversation.objects.select_for_update().get(interview=interview)
    key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    existing = row.receipts.filter(key=key).first()
    if existing:
        return (existing.response if existing.response.get('revision') == row.revision
                else reply(row, 'This command was already processed. Use the current state; do not repeat an earlier question.'))
    if row.receipts.count() >= 2000:
        raise ValidationError('Conversation command limit reached. Contact the recruiter for recovery.')
    action = args.get('action')
    if action not in ('current', 'answer', 'skip', 'repeat', 'pause', 'resume', 'finish', 'stop_early'):
        raise ValidationError('Unknown conversation action.')
    message = ''
    if row.stop_requested_at:
        return reply(row, 'Ending has already been requested. Do not issue another ending action.')
    if action == 'stop_early':
        explicit = re.fullmatch(r'(?:please |i want to |i would like to |can we )?(?:end|stop|quit|cancel) '
                                r'(?:the |this |my )?(?:interview|call)(?: now| please)?', clean(last_user(call)))
        if not explicit:
            message = 'Do not end the call. Ask the candidate to confirm explicitly if they want to stop the interview.'
        else:
            row.end_kind = 'CANDIDATE_ENDED'
    elif action == 'finish':
        turns = call.get('transcript_object') or []
        confirmations = [i for i,t in enumerate(turns) if isinstance(t,dict) and t.get('role') == 'agent'
                         and 'have you finished your answers' in clean(t.get('content', ''))]
        last_answer_index = max((p.get('turn_index', -1) for p in row.progress.values() if not p.get('source_interview')), default=-1)
        confirmation_asked = bool(confirmations and confirmations[-1] > last_answer_index
            and any(isinstance(t,dict) and t.get('role') == 'user' for t in turns[confirmations[-1]+1:]))
        confirmed = clean(last_user(call)) in ('yes i am finished', 'yes i m finished', 'i am finished', 'i m finished',
            'yes i am done', 'i am done', 'yes that is all', 'yes that s all', 'finish the interview')
        if row.cursor != len(row.questions) or row.paused or not confirmed or not confirmation_asked:
            message = 'Do not end the call. Resolve every question, then ask for the explicit confirmation "I am finished".'
        else:
            row.end_kind = 'COMPLETED'
    elif action == 'pause':
        row.paused = True
        message = 'Wait quietly. Keep this question active. Offer to repeat it when the candidate is ready; never treat noise as an answer.'
    elif action == 'resume':
        row.paused = False
    elif action in ('answer', 'skip'):
        if row.paused or row.cursor >= len(row.questions):
            message = 'Do not advance while paused or after the final question. Read the current state.'
        elif str(args.get('question_id')) != row.questions[row.cursor]['id']:
            message = 'The question ID is stale or foreign. Use the returned current question; do not advance.'
        else:
            users = candidate_segment(row, call)
            evidence = args.get('evidence')
            matched = (users[-1] if users and isinstance(evidence, str) and evidence.strip()
                       and evidence.strip() == users[-1][1].strip() else None)
            if action == 'skip':
                valid = matched and re.search(r'\b(?:skip (?:this |the )?question|skip this|pass on (?:this |the )?question|i (?:do not|don t) know)\b', clean(matched[1]))
            else:
                valid = matched and not is_clarification(matched[1])
            if not valid:
                message = 'No valid answer or explicit skip is evidenced after this question. Repeat or clarify the SAME question and wait.'
            else:
                row.progress[row.questions[row.cursor]['id']] = {
                    'status': 'ANSWER_CAPTURED' if action == 'answer' else 'EXPLICITLY_SKIPPED',
                    'turn_index': matched[0], 'evidence_hash': hashlib.sha256(matched[1].strip().encode()).hexdigest()}
                row.cursor += 1
    if row.cursor < len(row.questions) and row.progress[row.questions[row.cursor]['id']]['status'] == 'NOT_ASKED':
        row.progress[row.questions[row.cursor]['id']] = {'status': 'AWAITING_ANSWER'}
    if row.end_kind:
        row.stop_requested_at = timezone.now()
        transaction.on_commit(lambda: dispatch_stop(row.pk))
        message = 'The server has accepted the ending request. Do not ask any more questions.'
    row.revision += 1
    row.save()
    if action not in ('current', 'repeat'):
        from .audit import record_audit
        record_audit(actor=None, action='interview.conversation_' + action, target=row,
            changed_fields=['progress', 'paused', 'end_kind'], revision=row.revision,
            application_id=interview.application_id, interview_id=interview.pk)
    response = reply(row, message)
    ConversationReceipt.objects.create(conversation=row, key=key, response=response)
    return response


def dispatch_stop(interview_id):
    from .tasks.conversation import stop_conversation
    try:
        stop_conversation.apply_async(args=[str(interview_id)], retry=False)
    except Exception:
        # The intent stays in the database and periodic recovery republishes it.
        import logging
        logging.getLogger(__name__).warning('Conversation stop dispatch deferred interview_id=%s', interview_id)


def normal_completion_allowed(interview):
    row = InterviewConversation.objects.filter(interview=interview).first()
    return row is None or (row.end_kind == 'COMPLETED' and bool(row.questions) and row.cursor == len(row.questions)
        and len(row.progress) == len(row.questions)
        and all(p['status'] in ('ANSWER_CAPTURED', 'EXPLICITLY_SKIPPED') for p in row.progress.values()))


def prepare_continuation(previous, replacement):
    """Freeze verified prior evidence; never alter a historical call/transcript.

    Caller owns the workflow's Interview -> Application locks. The replacement
    retains the full bank for evaluation; the voice agent receives only remaining
    questions. A continuation requires authenticated, selected source evidence.
    """
    from .answer_evaluation import digest
    from django.conf import settings
    if not settings.RETELL_CONVERSATION_GUARD_ENABLED:
        raise ValidationError('Enable the deployed conversation guard before resuming remaining questions.')
    if previous.status != 'DONE' or not previous.completed_at:
        raise ValidationError('Only a verified ended attempt can be continued.')
    source = InterviewConversation.objects.filter(interview=previous).first()
    prep = getattr(previous, 'transcript_preparation', None)
    if not prep or not prep.selected_id:
        raise ValidationError('Selected authenticated transcript evidence is required before continuation.')
    transcript = prep.selected
    if digest({'text': transcript.text, 'turns': transcript.turns}) != transcript.content_hash:
        raise ValidationError('Prior transcript integrity check failed.')
    if not source:
        # Explicit recruiter-authorized legacy recovery. Infer only literal,
        # contiguous asked/answered segments from authenticated selected evidence.
        # Never infer that the candidate confirmed interview completion.
        source = initialize_locked(previous)
        raw = {'transcript_object': [{'role':t['role'], 'content':t['text']} for t in transcript.turns]}
        for question in source.questions:
            users = candidate_segment(source, raw)
            if not users or is_clarification(users[-1][1]):
                break
            index, text = users[-1]
            source.progress[question['id']] = {'status':'ANSWER_CAPTURED', 'turn_index':index,
                'evidence_hash':hashlib.sha256(text.strip().encode()).hexdigest()}
            source.cursor += 1
        source.save(update_fields=['progress', 'cursor', 'updated_at'])
    if source.end_kind == 'COMPLETED' or source.cursor >= len(source.questions):
        raise ValidationError('Only an interrupted interview with remaining questions can continue.')
    questions = [{'id': str(q.pk), 'text': q.text, 'category': q.category}
                 for q in replacement.questions.filter(approved=True).order_by('position', 'id')]
    if [(q['text'], q['category']) for q in questions] != [(q['text'], q['category']) for q in source.questions]:
        raise ValidationError('Continuation must retain the identical frozen question bank.')
    turns, progress = [], {q['id']: {'status': 'NOT_ASKED'} for q in questions}
    # Previously frozen carried evidence is preserved across multiple continuations.
    if source.carried_evidence:
        if digest(source.carried_evidence) != source.carried_hash:
            raise ValidationError('Prior continuation integrity check failed.')
        turns.extend(source.carried_evidence['turns'])
    for index in range(source.cursor):
        original = source.questions[index]
        state = source.progress[original['id']]
        progress[questions[index]['id']] = {**state, 'source_interview': str(previous.pk)}
        if state.get('source_interview'):
            continue  # Already represented by the frozen carried turns above.
        anchors = [i for i,t in enumerate(transcript.turns) if t['role'] == 'agent' and clean(original['text']) in clean(t['text'])]
        if not anchors:
            raise ValidationError('The selected transcript does not support a completed source question.')
        start = anchors[-1]
        end = next((i for i,t in enumerate(transcript.turns[start+1:], start+1) if t['role'] == 'user'
            and hashlib.sha256(t['text'].strip().encode()).hexdigest() == state.get('evidence_hash')), None)
        if end is None:
            raise ValidationError('The selected transcript does not contain the captured source answer.')
        turns.extend({**t, 'id': f'prior_{previous.pk}_{t["id"]}'} for t in transcript.turns[start:end+1])
    evidence = {'source_interview': str(previous.pk), 'source_transcript': str(transcript.pk),
                'source_hash': transcript.content_hash, 'turns': turns}
    return InterviewConversation.objects.create(interview=replacement, call_id='pending:' + str(replacement.pk),
        questions=questions, progress=progress, cursor=source.cursor,
        carried_evidence=evidence, carried_hash=digest(evidence))
