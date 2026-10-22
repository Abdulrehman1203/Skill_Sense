"""Versioned question/turn attribution, independent from grading.

Exact anchors are deterministic. Semantic proposals come from the provider and
must pass structural, literal-evidence, confidence and ambiguity checks. Unclear
attribution remains unavailable; question order alone never invents an answer.
"""
import re
from difflib import SequenceMatcher


def clean(text):
    return re.sub(r'\W+', ' ', text.lower()).strip()


def extract(inputs, proposals=None):
    from .answer_evaluation import InvalidEvaluation
    questions = inputs['context']['questions']
    turns = inputs['transcript']['turns']
    indices = {t['id']: i for i, t in enumerate(turns)}
    qids = {q['id'] for q in questions}
    if proposals is not None and (not isinstance(proposals, list) or len(proposals) != len(questions)):
        raise InvalidEvaluation('INVALID_ALIGNMENT')
    suggestions, seen = {}, set()
    for item in proposals or []:
        if (not isinstance(item, dict) or set(item) != {'question_id', 'asked_turn_id', 'confidence'}
                or item['question_id'] not in qids or item['question_id'] in seen
                or type(item['confidence']) not in (int, float) or not 0 <= item['confidence'] <= 1):
            raise InvalidEvaluation('INVALID_ALIGNMENT')
        seen.add(item['question_id'])
        tid = item['asked_turn_id']
        if tid is not None and (tid not in indices or turns[indices[tid]]['role'] != 'agent'):
            raise InvalidEvaluation('INVALID_ALIGNMENT_REFERENCE')
        suggestions[item['question_id']] = item
    anchors = {}
    for q in questions:
        exact = [i for i, t in enumerate(turns) if t['role'] == 'agent' and clean(q['text']) in clean(t['text'])]
        if len(exact) == 1:
            anchors[q['id']] = (exact[0], 1., 'EXACT')
        elif not exact:
            proposal = suggestions.get(q['id'])
            if proposal and proposal['asked_turn_id'] is not None and proposal['confidence'] >= .9:
                anchors[q['id']] = (indices[proposal['asked_turn_id']], proposal['confidence'], 'SEMANTIC')
            elif proposals is None:
                # High lexical equivalence also handles reordering/short paraphrases.
                scores = sorted([(SequenceMatcher(None, clean(q['text']), clean(t['text'])).ratio(), i)
                    for i, t in enumerate(turns) if t['role'] == 'agent'], reverse=True)
                if scores and scores[0][0] >= .9 and (len(scores) == 1 or scores[0][0] - scores[1][0] >= .15):
                    anchors[q['id']] = (scores[0][1], scores[0][0], 'LEXICAL')
    # One agent turn cannot silently stand for two independent asked questions.
    counts = {}
    for index, _, _ in anchors.values():
        counts[index] = counts.get(index, 0) + 1
    anchors = {qid: a for qid, a in anchors.items() if counts[a[0]] == 1}
    results, assigned = [], set()
    ordered_anchors = sorted(a[0] for a in anchors.values())
    for q in questions:
        anchor = anchors.get(q['id'])
        result = {'question_id': q['id'], 'state': 'UNALIGNABLE', 'confidence': 0., 'method': 'NONE',
            'asked_turn_id': None, 'answer_turn_ids': [], 'follow_up_turn_ids': [], 'spans': []}
        if anchor:
            start, confidence, method = anchor
            end = next((a for a in ordered_anchors if a > start), len(turns))
            segment = turns[start + 1:end]
            users = [t for t in segment if t['role'] == 'user']
            # Unrecognized question-like prompts may be skipped questions rather
            # than follow-ups. Do not attach their candidate speech by proximity.
            unclear = any(t['role'] == 'agent' and '?' in t['text'] and not re.match(
                r'(?i)^(can you (expand|clarify|elaborate)|why\b|how so\b|what (happened next|did you mean))', t['text'])
                for t in segment)
            final = end < len(turns) or inputs.get('final_evidence', False)
            state = 'UNALIGNABLE' if unclear else 'PARTIAL' if not final else 'ANSWERED' if users else 'NO_ANSWER'
            result.update(state=state, confidence=confidence, method=method, asked_turn_id=turns[start]['id'],
                answer_turn_ids=[t['id'] for t in users],
                follow_up_turn_ids=[t['id'] for t in segment if t['role'] == 'agent'])
            for t in [turns[start], *segment]:
                if state != 'UNALIGNABLE':
                    assigned.add(t['id'])
                result['spans'].append({'turn_id': t['id'], 'start_offset': 0, 'end_offset': len(t['text']),
                    'start_seconds': t.get('start_seconds'), 'end_seconds': t.get('end_seconds')})
        elif inputs.get('final_evidence') and not turns:
            result['state'] = 'NOT_ASKED'
        elif suggestions.get(q['id'], {}).get('asked_turn_id', 'unknown') is None and inputs.get('final_evidence'):
            result['state'] = 'NOT_ASKED'
        results.append(result)
    return {'questions': results, 'unmatched_turn_ids': [t['id'] for t in turns if t['id'] not in assigned]}
