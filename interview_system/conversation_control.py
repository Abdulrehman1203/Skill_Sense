"""Authenticated, evidence-checked dialogue progression. No grading by the agent.

Lock order: Interview -> InterviewConversation. A built-in end_call tool must NOT
be installed on the configured agent; the durable stop job owns normal hang-up.
"""
import hashlib
import json
import re
from datetime import timedelta
from difflib import SequenceMatcher

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .conversation_protocol import ANSWER_TOOL, CONTROL_TOOL
from .models import ConversationReceipt, Interview, InterviewConversation

PAUSE_REQUEST = re.compile(
    r'(?:please )?(?:pause|wait|hold on|one moment|i need (?:a moment|more time)|'
    r'give me (?:a moment|more time))(?: please)?'
)
RESUME_REQUEST = re.compile(
    r'(?:okay |ok )?(?:please )?(?:resume|continue|let s continue|i (?:am|m) ready(?: to continue)?)(?: now| please)?'
)
SKIP_REQUEST = re.compile(
    r'(?:please )?(?:(?:can|could) (?:i|we) )?(?:skip(?: (?:it|this|(?:this|the) question))?|'
    r'pass(?: on (?:this|the) question)?|i (?:do not|don t) know(?: the answer)?)(?: please)?'
)
STOP_REQUEST = re.compile(
    r'(?:please |i want to |i would like to |can we )?(?:end|stop|quit|cancel) '
    r'(?:the |this |my )?(?:interview|call)(?: now| please)?'
)
FINISH_PROMPT = re.compile(
    r'\b(?:have you finished (?:your |all (?:your )?)?answers?|'
    r'are you (?:finished|done)|do you need (?:any )?more time|'
    r'(?:please |just )?(?:say|confirm with)(?: the exact words| the same words)? i (?:am|m) finished)\b'
)
TOOL_TIMEOUT = re.compile(r'\btimeout of \d+ms exceeded\b', re.IGNORECASE)
FINISH_SPEECH = "Have you finished your answers, or do you need more time?"
FAREWELL_COMPLETE = "Thank you. Your interview is complete. Goodbye."
FAREWELL_EARLY = "Your interview has ended. Your available answers have been saved. Goodbye."


def clean(text):
    return re.sub(r'\W+', ' ', str(text).casefold()).strip()


def initialize_locked(interview):
    questions = list(interview.questions.filter(approved=True).order_by('position', 'created_at', 'pk').values('id', 'text', 'category'))
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


def snapshot(interview, *, include_live_question=False):
    row = InterviewConversation.objects.filter(interview=interview).first()
    if not row or (include_live_question and row.call_id != interview.retell_session_id):
        result = {'outcome': 'UNVERIFIED', 'answered': None, 'skipped': None, 'total': None, 'paused': False}
        if include_live_question:
            # Legacy agents receive the approved bank when the call is created.
            # The room matches their live utterances against that same ordering.
            result['approved_questions'] = list(interview.questions.filter(approved=True)
                .order_by('position', 'created_at', 'pk').values_list('text', flat=True))
            result['total'] = len(result['approved_questions'])
        return result
    terminal = interview.status == 'DONE' or bool(getattr(getattr(interview, 'session', None), 'audio_ended_at', None))
    outcome = ('COMPLETED' if row.end_kind == 'COMPLETED' else 'ENDED_EARLY' if row.end_kind == 'CANDIDATE_ENDED'
               else 'INTERRUPTED') if terminal else 'ENDING' if row.stop_requested_at else 'PAUSED' if row.paused else 'IN_PROGRESS'
    result = {'outcome': outcome, 'answered': sum(p['status'] == 'ANSWER_CAPTURED' for p in row.progress.values()),
              'skipped': sum(p['status'] == 'EXPLICITLY_SKIPPED' for p in row.progress.values()),
              'total': len(row.questions), 'paused': row.paused}
    if include_live_question and not terminal:
        from .live_transcript import current_answer
        result.update(
            question_number=row.cursor + 1 if row.cursor < len(row.questions) else None,
            question_text=row.questions[row.cursor]['text'] if row.cursor < len(row.questions) else None,
            live_answer=current_answer(row),
        )
    return result


def reply(row, message='Wait silently for the candidate.', *, speech_action='WAIT'):
    question = row.questions[row.cursor] if row.cursor < len(row.questions) else None
    speak_text = ''
    if question and speech_action in ('ASK_QUESTION', 'REPEAT_QUESTION'):
        speak_text = question['text']
    elif speech_action == 'ASK_FINISH':
        speak_text = FINISH_SPEECH
    elif speech_action == 'FAREWELL':
        speak_text = FAREWELL_COMPLETE if row.end_kind == 'COMPLETED' else FAREWELL_EARLY
    return {'revision': row.revision, 'current_question': question,
            'state': 'ENDING' if row.stop_requested_at else 'PAUSED' if row.paused else 'READY_TO_FINISH' if question is None else 'AWAITING_ANSWER',
            'speech_action': speech_action, 'speak_text': speak_text, 'instruction': message}


def _near_question(wording, spoken):
    expected, heard = wording.split(), spoken.split()
    if not 8 <= len(expected) <= 200 or not 6 <= len(heard) <= 400:
        return False
    matcher = SequenceMatcher(None, expected, heard, autojunk=False)
    blocks = matcher.get_matching_blocks()[:-1]
    if not blocks:
        return False
    span = blocks[-1].b + blocks[-1].size - blocks[0].b
    shared = sum(block.size for block in blocks)
    return shared / len(expected) >= .75 and 2 * shared / (len(expected) + span) >= .8


def candidate_segment(row, call, *, allow_transcription_variation=False):
    """Find candidate turns after the current question in the signed transcript.

    Keep a candidate's reply if the agent unnecessarily repeats the same
    question. A different question still closes the evidence window.
    """
    turns = call.get('transcript_object')
    if not isinstance(turns, list) or len(turns) > 10000:
        return []
    after_index = max((entry.get('turn_index', -1) for entry in row.progress.values()
                       if not entry.get('source_interview')), default=-1)
    return question_segment(turns, row.questions, row.cursor, after_index, allow_transcription_variation)[1]


def question_segment(turns, questions, cursor, after_index, allow_variation, *, first_anchor=False):
    if cursor >= len(questions) or not (wording := clean(questions[cursor]['text'])):
        return None, []
    other_questions = {clean(q['text']) for i, q in enumerate(questions) if i != cursor} - {wording, ''}
    anchor_start = None
    users = []
    agent_fragments = []
    for index in range(after_index + 1, len(turns)):
        turn = turns[index]
        if not isinstance(turn, dict):
            continue
        text = turn.get('content', '')
        if not isinstance(text, str):
            continue
        if turn.get('role') == 'agent':
            spoken = clean(text)
            agent_fragments = [*agent_fragments[-2:], (index, spoken)]
            window = ' '.join(fragment for _, fragment in agent_fragments)
            other = any(q and q in window for q in other_questions)
            if allow_variation and wording not in window and not other:
                other = any(_near_question(q, window) for q in other_questions)
            if other:
                anchor_start = None
                users.clear()
                agent_fragments.clear()
            elif wording in spoken or (
                allow_variation and (wording in window or _near_question(wording, window))
            ):
                if not users:
                    anchor_start = index if wording in spoken else agent_fragments[0][0]
                    if first_anchor:
                        return anchor_start, []
                agent_fragments.clear()
        elif turn.get('role') == 'user':
            agent_fragments.clear()
            if anchor_start is not None and clean(text):
                users.append((index, text))
    return anchor_start, users


def is_clarification(text):
    value = clean(text)
    return bool(
        PAUSE_REQUEST.fullmatch(value)
        or RESUME_REQUEST.fullmatch(value)
        or explicit_repeat(text)
        or any(phrase in value for phrase in ('cannot hear', 'can t hear', 'didn t hear', 'did not hear'))
        or value in {
            '', 'yes', 'no', 'okay', 'ok', 'sorry', 'pardon', 'silence', 'cough', 'coughing',
            'noise', 'background noise', 'unintelligible', 'unintelligible audio', 'inaudible', 'inaudible audio',
        }
    )


def candidate_quotes(users, *, combine=False):
    if not users:
        return
    index, latest = users[-1]
    yield index, latest
    if combine and not non_answer(latest):
        parts = [latest]
        for previous_index, text in reversed(users[-4:-1]):
            if previous_index != index - len(parts) or non_answer(text):
                break
            parts.insert(0, text)
            yield index, ' '.join(parts)


def matched_evidence(users, evidence, *, combine=False):
    if not isinstance(evidence, str) or not (quote := clean(evidence)):
        return None
    # The evidence must end at the latest signed candidate turn.
    return next(((index, text) for index, text in candidate_quotes(users, combine=combine)
                 if clean(text) == quote), None)


def matched_answer(users, args):
    if args.get('_signed_answer'):
        quotes = list(candidate_quotes(users, combine=True))
        return quotes[-1] if quotes and not non_answer(quotes[0][1]) else None
    if 'answer_tail' not in args:
        return matched_evidence(users, args.get('evidence'), combine=True)
    tail = args['answer_tail']
    if not isinstance(tail, str) or 'evidence' in args or not 1 <= len(tail) <= 256:
        return None
    words = clean(tail).split()
    if not 1 <= len(words) <= 12:
        return None
    # The agent proves it saw the latest turn with a short literal suffix.
    # Capture the original signed utterance(s), never the agent's quote.
    quotes = list(candidate_quotes(users, combine=True))
    if not quotes or non_answer(quotes[0][1]):
        return None
    if len(words) < 4:
        return quotes[0] if clean(quotes[0][1]).split() == words else None
    return next(((index, text) for index, text in reversed(quotes)
                 if clean(text).split()[-len(words):] == words), None)


def tool_arguments(name, args):
    if not isinstance(args, dict):
        return None
    if name == CONTROL_TOOL:
        return args
    if name == ANSWER_TOOL and set(args) in ({'question_id'}, {'question_id', 'answer_tail'}):
        return {'question_id': args['question_id'], 'action': 'answer', '_signed_answer': True}
    return None


def timed_out_answer(call):
    """Recover only the latest answer/skip that Retell reports as timed out."""
    trace = call.get('transcript_with_tool_calls')
    if not isinstance(trace, list) or len(trace) > 10000:
        return None
    results = (turn for turn in reversed(trace)
               if isinstance(turn, dict) and turn.get('role') == 'tool_call_result')
    result = next(results, {})
    tool_id, content = result.get('tool_call_id'), result.get('content')
    if (not isinstance(tool_id, str) or not tool_id or result.get('successful') is not False
            or not isinstance(content, str) or not TOOL_TIMEOUT.search(content)):
        return None
    for turn in reversed(trace):
        if (isinstance(turn, dict) and turn.get('role') == 'tool_call_invocation'
                and turn.get('tool_call_id') == tool_id):
            args = turn.get('arguments')
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    return None
            args = tool_arguments(turn.get('name'), args)
            if args and args.get('action') in ('answer', 'skip'):
                return args
            return None
    return None


def recover_prompt(row, call, args):
    if not row.cursor:
        return False
    previous = row.progress[row.questions[row.cursor - 1]['id']]
    turns = call.get('transcript_object')
    index = previous.get('turn_index', -1)
    if (row.paused or args.get('question_id') != row.questions[row.cursor - 1]['id']
            or previous.get('source_interview') or previous.get('delivery_recovered')
            or not isinstance(turns, list) or not 0 <= index < len(turns) <= 10000
            or not isinstance(turns[index], dict) or turns[index].get('role') != 'user'):
        return False
    if row.cursor < len(row.questions):
        anchor, _ = question_segment(turns, row.questions, row.cursor, index, True, first_anchor=True)
        if anchor is not None:
            return False
    elif closing_prompt_spoken(turns[index + 1:]):
        return False
    previous['delivery_recovered'] = True
    return True


def closing_prompt_spoken(turns):
    agent_words = []
    for turn in turns:
        if not isinstance(turn, dict) or not isinstance(turn.get('content'), str):
            continue
        if turn.get('role') == 'user':
            agent_words.clear()
        elif turn.get('role') == 'agent':
            agent_words.append(turn['content'])
            if FINISH_PROMPT.search(clean(' '.join(agent_words))):
                return True
    return False


def explicit_repeat(text):
    return bool(re.fullmatch(
        r'(?:sorry )?(?:(?:can|could|would|will) you )?(?:please )?'
        r'(?:repeat(?: it| that| the question| this question)?|say (?:it|that|the question) again|'
        r'what (?:is|was) the question|ask (?:me )?the question)'
        r'(?: please| again)?',
        clean(text),
    ))


def non_answer(text):
    wording = clean(text)
    return is_clarification(text) or bool(SKIP_REQUEST.fullmatch(wording) or STOP_REQUEST.fullmatch(wording))


def last_user(call):
    turns = call.get('transcript_object') or []
    if not isinstance(turns, list):
        return ''
    return next((t.get('content', '') for t in reversed(turns) if isinstance(t, dict) and t.get('role') == 'user'), '')


def repeat_recovery_turn(row, call):
    """Allow an explicit replay request when the first tool response was lost."""
    turns = call.get('transcript_object')
    if not isinstance(turns, list) or not turns or len(turns) > 10000:
        return []
    turn = turns[-1]
    index = len(turns) - 1
    if (not isinstance(turn, dict) or turn.get('role') != 'user'
            or not isinstance(turn.get('content'), str)
            or any(p.get('turn_index', -1) >= index for p in row.progress.values()
                   if not p.get('source_interview'))):
        return []
    return [(index, turn['content'])]


def finish_confirmed(row, call):
    """Require a closing prompt and explicit consent after the last answer.

    Provider transcripts split speech at interruptions, so match adjacent agent
    fragments together and allow natural wording rather than a magic sentence.
    """
    turns = call.get('transcript_object')
    if not isinstance(turns, list) or len(turns) > 10000:
        return False
    last_answer = max((p.get('turn_index', -1) for p in row.progress.values()
                       if not p.get('source_interview')), default=-1)
    prompted, confirmed, agent_words, candidate_words = False, False, [], []
    for turn in turns[last_answer + 1:]:
        if not isinstance(turn, dict) or not isinstance(turn.get('content'), str):
            continue
        if turn.get('role') == 'agent':
            candidate_words = []
            agent_words.append(turn['content'])
            wording = clean(' '.join(agent_words))
            prompted = prompted or bool(FINISH_PROMPT.search(wording))
        elif turn.get('role') == 'user':
            agent_words = []
            candidate_words.append(turn['content'])
            value = clean(turn['content'])
            if re.search(r'\b(?:not (?:finished|done)|need more time|keep going|more to add)\b', value):
                confirmed = False
            elif prompted and any(re.fullmatch(
                r'(?:(?:yes|yeah|yep|okay|ok|uh|um) )*'
                r'(?:i (?:(?:am|m) (?:finished|done)|(?:have )?finished(?: my answers?)?)|'
                r'my answers? (?:is|are) complete|that (?:is|s) all|finish the interview)'
                r'(?: (?:thank you|thanks|please))?',
                clean(text),
            ) for text in (turn['content'], ' '.join(candidate_words[-3:]))):
                confirmed = True
    return prompted and confirmed


@transaction.atomic
def control(payload):
    call = payload.get('call')
    args = tool_arguments(payload.get('name'), payload.get('args'))
    if not isinstance(call, dict) or args is None:
        raise ValidationError('Invalid conversation command.')
    if not isinstance(call.get('call_id'), str) or not 1 <= len(call['call_id']) <= 255:
        raise ValidationError('Invalid provider call identity.')
    if set(args) - {'action', 'question_id', 'evidence', 'answer_tail', '_signed_answer'}:
        raise ValidationError('Unsupported conversation arguments.')
    if payload.get('name') == CONTROL_TOOL and '_signed_answer' in args:
        raise ValidationError('Unsupported conversation arguments.')
    interview = Interview.objects.select_for_update().filter(retell_session_id=call.get('call_id')).first()
    if interview is None or interview.status != 'LIVE':
        raise ValidationError('Call is not an active interview.')
    row = InterviewConversation.objects.select_for_update().filter(interview=interview).first()
    if row is None or row.call_id != interview.retell_session_id:
        row = initialize_locked(interview)
    action = args.get('action')
    if action not in ('current', 'answer', 'skip', 'repeat', 'pause', 'resume', 'finish', 'stop_early', 'recover'):
        raise ValidationError('Unknown conversation action.')
    if row.stop_requested_at:
        return reply(row, 'Ending has already been requested. Remain silent; the server will end the call.')
    if action == 'current':
        if row.paused:
            return reply(row)
        if row.cursor < len(row.questions):
            if row.progress[row.questions[row.cursor]['id']]['status'] != 'NOT_ASKED':
                return reply(row)
            turns = call.get('transcript_object')
            if isinstance(turns, list) and len(turns) <= 10000:
                anchor, _ = question_segment(turns, row.questions, row.cursor, -1, True, first_anchor=True)
                if anchor is not None:
                    return reply(row)
        elif row.progress[row.questions[-1]['id']].get('finish_prompt_issued'):
            return reply(row)
    key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    existing = row.receipts.filter(key=key).first()
    if existing:
        return reply(row, 'This command was already processed. Wait silently; do not replay its speech.')
    if row.receipts.count() >= 2000:
        raise ValidationError('Conversation command limit reached. Contact the recruiter for recovery.')
    message = ''
    speech_action = 'WAIT'
    recovery = action == 'recover'
    recovered_prompt = False
    if recovery:
        args = timed_out_answer(call)
        if not args:
            return reply(row, 'There is no timed-out answer to recover. Wait silently.')
        action = args['action']
        recovered_prompt = recover_prompt(row, call, args)
    if action == 'stop_early':
        explicit = STOP_REQUEST.fullmatch(clean(last_user(call)))
        if not explicit:
            message = 'Do not end the call. Ask the candidate to confirm explicitly if they want to stop the interview.'
        else:
            row.end_kind = 'CANDIDATE_ENDED'
    elif action == 'finish':
        if row.cursor != len(row.questions) or row.paused or not finish_confirmed(row, call):
            message = 'Ending has not been authorized. Wait silently for the candidate to continue.'
        else:
            row.end_kind = 'COMPLETED'
    elif action in ('pause', 'resume'):
        if row.cursor < len(row.questions):
            question = row.questions[row.cursor]
            users = candidate_segment(row, call, allow_transcription_variation=True)
            matched = matched_evidence(users, args.get('evidence'))
            progress = row.progress[question['id']]
            pattern = PAUSE_REQUEST if action == 'pause' else RESUME_REQUEST
            if (args.get('question_id') == question['id'] and matched
                    and matched[0] > progress.get('pause_turn_index', -1)
                    and pattern.fullmatch(clean(matched[1]))):
                row.paused = action == 'pause'
                progress['pause_turn_index'] = matched[0]
        message = 'Wait silently. Keep the current question active.'
    elif action == 'repeat':
        question = row.questions[row.cursor] if row.cursor < len(row.questions) else None
        if question and not row.paused and args.get('question_id') == question['id']:
            progress = row.progress[question['id']]
            users = candidate_segment(row, call, allow_transcription_variation=True)
            opening = call.get('retell_llm_dynamic_variables')
            if not users and (progress['status'] == 'AWAITING_ANSWER'
                              or (isinstance(opening, dict) and opening.get('opening_question_id') == question['id'])):
                users = repeat_recovery_turn(row, call)
            matched = matched_evidence(users, args.get('evidence'))
            if matched and explicit_repeat(matched[1]) and matched[0] > progress.get('repeat_turn_index', -1):
                progress['repeat_turn_index'] = matched[0]
                speech_action = 'REPEAT_QUESTION'
                message = 'Repeat the current question verbatim once, then wait silently.'
    elif action in ('answer', 'skip') and not recovered_prompt:
        if row.paused or row.cursor >= len(row.questions):
            message = 'Do not advance while paused or after the final question. Read the current state.'
        elif str(args.get('question_id')) != row.questions[row.cursor]['id']:
            message = 'The question ID is stale or foreign. Wait silently; do not advance or repeat.'
        else:
            users = candidate_segment(row, call, allow_transcription_variation=True)
            matched = matched_answer(users, args) if action == 'answer' else matched_evidence(users, args.get('evidence'))
            wording = clean(matched[1]) if matched else ''
            if action == 'skip':
                valid = SKIP_REQUEST.fullmatch(wording)
            else:
                valid = matched and not non_answer(matched[1])
            if not valid:
                message = 'The latest answer or skip could not be verified. Wait for the candidate; do not repeat the question.'
            else:
                row.progress[row.questions[row.cursor]['id']] = {
                    'status': 'ANSWER_CAPTURED' if action == 'answer' else 'EXPLICITLY_SKIPPED',
                    'turn_index': matched[0], 'evidence_hash': hashlib.sha256(matched[1].strip().encode()).hexdigest()}
                if recovery:
                    row.progress[row.questions[row.cursor]['id']]['delivery_recovered'] = True
                row.cursor += 1
    if not message and not row.paused and not row.end_kind and action in ('current', 'answer', 'skip'):
        if row.cursor < len(row.questions):
            progress = row.progress[row.questions[row.cursor]['id']]
            if recovered_prompt or progress['status'] == 'NOT_ASKED':
                progress['status'] = 'AWAITING_ANSWER'
                speech_action = 'ASK_QUESTION'
                message = 'Ask the current question verbatim once, then wait silently.'
        else:
            progress = row.progress[row.questions[-1]['id']]
            if recovered_prompt or not progress.get('finish_prompt_issued'):
                progress['finish_prompt_issued'] = True
                speech_action = 'ASK_FINISH'
                message = 'Ask once: "Have you finished your answers, or do you need more time?" Then wait for explicit confirmation.'
    if row.end_kind:
        speech_action = 'FAREWELL'
        row.stop_requested_at = timezone.now()
        # The tool response precedes spoken playback. Never race the closing
        # sentence with the stop API. Persist the deadline for broker recovery.
        row.next_stop_at = row.stop_requested_at + timedelta(seconds=10)
        transaction.on_commit(lambda: dispatch_stop(row.pk))
        message = ('Say exactly: "Thank you. Your interview is complete. Goodbye." Then remain silent. '
                   'Do not ask questions or call any more tools. The server will end the call shortly.'
                   if row.end_kind == 'COMPLETED' else
                   'Say exactly: "Your interview has ended. Your available answers have been saved. Goodbye." '
                   'Then remain silent. Do not ask questions or call any more tools.')
    row.revision += 1
    row.save()
    if action not in ('current', 'repeat'):
        from .audit import record_audit
        record_audit(actor=None, action='interview.conversation_' + ('recover' if recovery else action), target=row,
            changed_fields=['progress', 'paused', 'end_kind'], revision=row.revision,
            application_id=interview.application_id, interview_id=interview.pk)
    response = reply(row, message or 'Wait silently for the candidate.', speech_action=speech_action)
    ConversationReceipt.objects.create(conversation=row, key=key, response=response)
    return response


def dispatch_stop(interview_id):
    from .tasks.conversation import stop_conversation
    try:
        deadline = InterviewConversation.objects.values_list('next_stop_at', flat=True).get(pk=interview_id)
        stop_conversation.apply_async(args=[str(interview_id)], eta=deadline, retry=False)
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
                 for q in replacement.questions.filter(approved=True).order_by('position', 'created_at', 'pk')]
    if [(q['text'], q['category']) for q in questions] != [(q['text'], q['category']) for q in source.questions]:
        raise ValidationError('Continuation must retain the identical frozen question bank.')
    turns, progress = [], {q['id']: {'status': 'NOT_ASKED'} for q in questions}
    # Previously frozen carried evidence is preserved across multiple continuations.
    if source.carried_evidence:
        if digest(source.carried_evidence) != source.carried_hash:
            raise ValidationError('Prior continuation integrity check failed.')
        turns.extend(source.carried_evidence['turns'])
    source_turns = [{'role': t['role'], 'content': t['text']} for t in transcript.turns]
    last_captured = -1
    for index in range(source.cursor):
        original = source.questions[index]
        state = source.progress[original['id']]
        progress[questions[index]['id']] = {**state, 'source_interview': str(previous.pk)}
        if state.get('source_interview'):
            continue  # Already represented by the frozen carried turns above.
        captured = None
        consecutive = []
        for end in range(last_captured + 1, len(source_turns)):
            turn = source_turns[end]
            if turn['role'] != 'user':
                consecutive.clear()
                continue
            consecutive = [*consecutive[-3:], (end, turn['content'])]
            if not any(hashlib.sha256(quote.strip().encode()).hexdigest() == state.get('evidence_hash')
                       for _, quote in candidate_quotes(consecutive, combine=True)):
                continue
            start, users = question_segment(source_turns[:end + 1], source.questions, index, last_captured, True)
            if start is not None and any(
                i == end and hashlib.sha256(quote.strip().encode()).hexdigest() == state.get('evidence_hash')
                for i, quote in candidate_quotes(users, combine=True)
            ):
                captured = (start, end)
                break
        if captured is None:
            raise ValidationError('The selected transcript does not contain the captured source answer.')
        start, end = captured
        last_captured = end
        turns.extend({**t, 'id': f'prior_{previous.pk}_{t["id"]}'} for t in transcript.turns[start:end+1])
    evidence = {'source_interview': str(previous.pk), 'source_transcript': str(transcript.pk),
                'source_hash': transcript.content_hash, 'turns': turns}
    return InterviewConversation.objects.create(interview=replacement, call_id='pending:' + str(replacement.pk),
        questions=questions, progress=progress, cursor=source.cursor,
        carried_evidence=evidence, carried_hash=digest(evidence))
