"""Frozen feedback-v1 templates. Never interpolate job, candidate or model prose.

Only validated scores/evidence availability select statements. New wording or
projection policies require a new contract version, not edits to released reports.
"""
from decimal import Decimal, ROUND_HALF_UP

DIMENSIONS = {
    ('TECHNICAL', 'accuracy'): ('Technical accuracy', 'The assessed answers demonstrated technical accuracy.',
        'The assessed answers need stronger technical accuracy.', 'Review the core concepts for this role and practice explaining a small working example.'),
    ('TECHNICAL', 'reasoning'): ('Technical reasoning', 'The assessed answers demonstrated supported technical reasoning.',
        'The assessed answers need a clearer explanation of technical reasoning.', 'Practice explaining your assumptions, alternatives, trade-offs and how you would verify the result.'),
    ('BEHAVIORAL', 'specificity'): ('Specific examples', 'The assessed examples included concrete context and actions.',
        'The assessed examples need more specific context and actions.', 'Prepare an example using situation, task, your own actions and an observable result.'),
    ('BEHAVIORAL', 'reflection'): ('Reflection and collaboration', 'The assessed examples demonstrated supported reflection and collaboration.',
        'The assessed examples need clearer reflection on actions and outcomes.', 'Explain what you learned, how you worked with others and what you would change next time.'),
    ('SITUATIONAL', 'judgment'): ('Situational judgment', 'The assessed scenarios demonstrated consideration of priorities and risks.',
        'The assessed scenarios need clearer prioritization and risk consideration.', 'Practice stating your first action, the main risk and who you would consult before proceeding.'),
    ('SITUATIONAL', 'reasoning'): ('Situational reasoning', 'The assessed scenarios explained options and trade-offs.',
        'The assessed scenarios need a clearer explanation of options and assumptions.', 'Compare two possible approaches and explain the assumptions that would change your choice.'),
}


def percentage(value):
    return float(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def project(context, answers, score, coverage):
    questions = {q['id']: q for q in context['questions']}
    measurements = {}
    assessed = 0
    for answer in answers:
        if answer['status'] == 'MISSING_EVIDENCE':
            continue
        assessed += 1
        category = questions[answer['question_id']]['category']
        for criterion, value in answer['criteria'].items():
            key = (category, criterion)
            if key not in DIMENSIONS:
                raise ValueError('UNSUPPORTED_FEEDBACK_CRITERION')
            measurements.setdefault(key, []).append(Decimal(str(value)))
    dimensions, strengths, gaps, suggestions = [], [], [], []
    for key in sorted(measurements):
        values = measurements[key]
        average = sum(values) / len(values)
        label, strength, gap, action = DIMENSIONS[key]
        dimension_id = f'{key[0].lower()}.{key[1]}'
        dimensions.append({'id': dimension_id, 'label': label, 'percentage': percentage(average * 100), 'assessed_answers': len(values)})
        if average >= Decimal('.75'):
            strengths.append({'dimension_id': dimension_id, 'description': strength})
        elif average <= Decimal('.5'):
            gaps.append({'dimension_id': dimension_id, 'description': gap})
        if average < Decimal('.75'):
            suggestions.append({'dimension_id': dimension_id, 'action': action})
    return {'interview_summary': {'text': f'This feedback covers {assessed} of {len(questions)} interview questions with scorable evidence.',
        'performance_percentage': percentage(score), 'answer_coverage': coverage,
        'assessed_questions': assessed, 'total_questions': len(questions)},
        'performance_dimensions': dimensions, 'strengths': strengths, 'gaps': gaps,
        'improvement_suggestions': suggestions,
        'limitations': ['Only the available answer evidence is assessed. Missing evidence is not a zero score.',
                        'These are interview development suggestions, not a hiring decision.']}
