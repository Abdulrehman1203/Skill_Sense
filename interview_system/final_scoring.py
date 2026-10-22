"""Current combined recommendation policy and durable, revision-keyed orchestration.

Lock order for score workers: Interview -> Application -> policy singleton -> job.
Rubric activation locks policy only; its rebuild scan never holds it across apps.
Evaluation completion already holds Interview and may acquire Application here.
"""
from django.db import transaction
from django.db.models import Max

from ai.scoring.rubric import combined_score
from .answer_evaluation import digest
from .models import Application, CandidateScore, Interview, ScoreRevision, ScoringPolicyState


def current_inputs(app):
    assessment = app.current_assessment
    state = ScoringPolicyState.objects.select_related('active_rubric').filter(pk=1).first()
    rubric = state.active_rubric if state else None
    interview = app.interviews.order_by('-created_at', '-pk').select_related('question_set').first()
    evaluation = interview.evaluations.order_by('-revision').first() if interview else None
    match = float(assessment.match_score) if (assessment and assessment.status == 'SUCCEEDED'
        and assessment.resume_id == app.resume_id and assessment.match_score is not None) else None
    reason = ''
    performance = None
    if match is None:
        reason = 'SCREENING_UNAVAILABLE'
    elif not evaluation or evaluation.status in ('QUEUED', 'PROCESSING'):
        reason = 'EVALUATION_PENDING'
    elif (not interview.question_set_id or interview.question_set.assessment_id != assessment.pk):
        reason = 'ASSESSMENT_EVALUATION_MISMATCH'
    elif (interview.status != 'DONE' or not interview.completed_at or interview.completion_outcome != 'USABLE' or interview.evaluation_status != 'READY'
          or evaluation.context.interview_id != interview.pk or evaluation.transcript.interview_id != interview.pk
          or evaluation.status != 'READY' or evaluation.coverage != 1 or evaluation.score is None or not evaluation.published_at):
        reason = 'EVALUATION_UNAVAILABLE'
    else:
        performance = evaluation.score  # Already a percentage, not a normalized criterion.
    from .conversation_control import normal_completion_allowed
    if interview and not normal_completion_allowed(interview):
        reason, performance = 'INTERVIEW_INTERRUPTED', None
    if not rubric or not rubric.active or rubric.policy_version != 'COMBINED_V1':
        reason = 'SCORING_POLICY_UNAVAILABLE'
    inputs = {'policy': 'COMBINED_V1', 'application': str(app.pk),
        'assessment': str(assessment.pk) if assessment else None,
        'assessment_revision': assessment.revision if assessment else None,
        'evaluation': str(evaluation.pk) if evaluation else None,
        'evaluation_revision': evaluation.revision if evaluation else None,
        'evaluation_coverage': evaluation.coverage if evaluation else None,
        'transcript_hash': evaluation.transcript.content_hash if evaluation else None,
        'context_hash': evaluation.context.content_hash if evaluation else None,
        'rubric': str(rubric.pk) if rubric else None, 'rubric_version': rubric.version if rubric else None,
        'weights': ({'match': str(rubric.weight_match), 'interview': str(rubric.weight_interview), 'behavioral': str(rubric.weight_behavioral)} if rubric else None),
        'screening_match_percentage': match, 'interview_performance_percentage': performance,
        'reason': reason}
    return inputs, assessment, evaluation, rubric


def ensure_locked(app):
    """Persist terminal evaluation intent in the caller's transaction before dispatch."""
    inputs, assessment, evaluation, rubric = current_inputs(app)
    # Pending reads need no fabricated score row. Once terminal evidence exists,
    # preserve both complete and blocked attempts as explicit history.
    if not (assessment and assessment.status == 'SUCCEEDED' and evaluation and rubric and rubric.policy_version == 'COMBINED_V1'
            and (evaluation.published_at or evaluation.status in ('FAILED', 'UNAVAILABLE'))):
        return None
    key = digest(inputs)
    row = ScoreRevision.objects.filter(key=key).first()
    if row:
        CandidateScore.objects.update_or_create(application=app, defaults={'current_revision': row,
            'final_score': row.final_score if row.status == 'COMPLETE' else None,
            'breakdown': {'match': inputs['screening_match_percentage'], 'interview': inputs['interview_performance_percentage'], 'behavioral': None},
            'explanation': 'Current combined recommendation.' if row.status == 'COMPLETE' else 'Final recommendation pending or unavailable.'})
        return row
    result = combined_score(inputs['screening_match_percentage'], inputs['interview_performance_percentage'],
        weight_match=rubric.weight_match, weight_interview=rubric.weight_interview, weight_behavioral=rubric.weight_behavioral)
    row = ScoreRevision.objects.create(application=app, assessment=assessment, evaluation=evaluation, rubric=rubric,
        number=(app.score_revisions.aggregate(n=Max('number'))['n'] or 0) + 1, key=key, inputs=inputs,
        status='INCOMPLETE' if inputs['reason'] else 'QUEUED', error_code=inputs['reason'],
        screening_match_percentage=result['screening_match_percentage'],
        interview_performance_percentage=result['interview_performance_percentage'], coverage=result['coverage'])
    CandidateScore.objects.update_or_create(application=app, defaults={'current_revision': row,
        'final_score': None, 'breakdown': {'match': inputs['screening_match_percentage'],
        'interview': inputs['interview_performance_percentage'], 'behavioral': None},
        'explanation': 'Required evidence is incomplete.' if inputs['reason'] else 'Combined recommendation pending.'})
    if row.status == 'QUEUED':
        transaction.on_commit(lambda: dispatch(row.pk))
    return row


@transaction.atomic
def ensure(application_id):
    app = Application.objects.select_for_update().get(pk=application_id)
    return ensure_locked(app)


def dispatch(pk):
    from .tasks.scoring_tasks import calculate_score_revision
    try:
        calculate_score_revision.apply_async(args=[str(pk)], retry=False)
    except Exception:
        import logging
        logging.getLogger(__name__).warning('Score dispatch deferred score_revision=%s', pk)


def lock_sources(row):
    # Caller must be atomic. A concurrent schedule is caught by the freshness
    # comparison after the application lock; scheduling itself locks Application.
    Interview.objects.select_for_update().get(pk=row.evaluation.interview_id)
    app = Application.objects.select_for_update().get(pk=row.application_id)
    ScoringPolicyState.objects.select_for_update().get(pk=1)
    return app


def payload(app):
    inputs, _, _, _ = current_inputs(app)
    key = digest(inputs)
    row = app.score_revisions.filter(key=key).first()
    complete = bool(row and row.status == 'COMPLETE' and not inputs['reason'])
    coverage = row.coverage if row else (combined_score(inputs['screening_match_percentage'], inputs['interview_performance_percentage'],
        weight_match=inputs['weights']['match'], weight_interview=inputs['weights']['interview'],
        weight_behavioral=inputs['weights']['behavioral'])['coverage'] if inputs['weights'] and inputs['reason'] != 'SCORING_POLICY_UNAVAILABLE' else None)
    result = {'status': 'COMPLETE' if complete else row.status if row and not inputs['reason'] else 'INCOMPLETE' if inputs['reason'] else 'PENDING',
        'policy_version': 'COMBINED_V1', 'score_revision': str(row.pk) if row else None,
        'revision': row.number if row else None, 'rubric': inputs['rubric'], 'rubric_version': inputs['rubric_version'],
        'weights': inputs['weights'], 'source_revisions': {k: inputs[k] for k in ('assessment', 'assessment_revision', 'evaluation', 'evaluation_revision')},
        'screening_match_percentage': inputs['screening_match_percentage'],
        'interview_performance_percentage': inputs['interview_performance_percentage'],
        'combined_recommendation_percentage': float(row.final_score) if complete else None,
        'final_score': float(row.final_score) if complete else None,
        'coverage': float(coverage) if coverage is not None else None,
        'answer_coverage': inputs['evaluation_coverage'],
        'error_code': inputs['reason'] or (row.error_code if row else ''),
        'completed_at': row.completed_at if complete else None,
        'breakdown': {'match': inputs['screening_match_percentage'], 'interview': inputs['interview_performance_percentage'], 'behavioral': None},
        'explanation': 'Combined recommendation from screening and evaluated answers; behavioral observations excluded.' if complete else 'Final recommendation is pending or unavailable; missing evidence is not zero.'}
    return result


@transaction.atomic
def activate_rubric(pk):
    from .models import ScoringRubric
    state, _ = ScoringPolicyState.objects.get_or_create(pk=1)
    state = ScoringPolicyState.objects.select_for_update().get(pk=1)
    rubric = ScoringRubric.objects.get(pk=pk)
    if rubric.policy_version != 'COMBINED_V1':
        from rest_framework.exceptions import ValidationError
        raise ValidationError('Legacy rubrics cannot be activated for new scoring.')
    combined_score(None, None, weight_match=rubric.weight_match, weight_interview=rubric.weight_interview,
        weight_behavioral=rubric.weight_behavioral)
    if state.active_rubric_id == rubric.pk and rubric.active:
        return rubric, False
    ScoringRubric.objects.filter(active=True).update(active=False)
    ScoringRubric.objects.filter(pk=pk).update(active=True)
    state.active_rubric, state.epoch = rubric, state.epoch + 1
    state.rebuild_pending, state.rebuild_cursor = True, None
    state.save()
    rubric.active = True
    return rubric, True


def ranking_queryset(job=None):
    """Only the same active policy and current complete evidence have sortable values."""
    from django.db.models import Case, DecimalField, F, IntegerField, OuterRef, Q, Subquery, Value, When
    from .models import InterviewEvaluation
    state = ScoringPolicyState.objects.select_related('active_rubric').filter(pk=1).first()
    rubric_id = state.active_rubric_id if state else None
    latest_interview = Interview.objects.filter(application_id=OuterRef('pk')).order_by('-created_at', '-pk')
    query = Application.objects.all()
    if job is not None:
        query = query.filter(job=job)
    query = query.annotate(_latest_interview=Subquery(latest_interview.values('pk')[:1]))
    latest_eval = InterviewEvaluation.objects.filter(interview_id=OuterRef('_latest_interview')).order_by('-revision')
    query = query.annotate(_latest_evaluation=Subquery(latest_eval.values('pk')[:1]))
    eligible = Q(score__current_revision__status='COMPLETE', score__current_revision__rubric_id=rubric_id,
        score__current_revision__rubric__active=True, score__current_revision__rubric__policy_version='COMBINED_V1',
        score__current_revision__assessment_id=F('current_assessment_id'), current_assessment__status='SUCCEEDED',
        current_assessment__resume_id=F('resume_id'), current_assessment__match_score__isnull=False,
        score__current_revision__evaluation_id=F('_latest_evaluation'), score__current_revision__evaluation__status='READY',
        score__current_revision__evaluation__coverage=1, score__current_revision__evaluation__interview__status='DONE',
        score__current_revision__evaluation__published_at__isnull=False,
        score__current_revision__evaluation__score__isnull=False,
        score__current_revision__evaluation__context_id=F('_latest_interview'),
        score__current_revision__evaluation__transcript__interview_id=F('_latest_interview'),
        score__current_revision__evaluation__interview__completed_at__isnull=False,
        score__current_revision__evaluation__interview__evaluation_status='READY',
        score__current_revision__evaluation__interview__completion_outcome='USABLE',
        score__current_revision__evaluation__interview__question_set__assessment_id=F('current_assessment_id'))
    return query.annotate(_ranking_rubric_version=Value(state.active_rubric.version if state and state.active_rubric else None,
        output_field=IntegerField()),
        _rank_score=Case(When(eligible, then=F('score__current_revision__final_score')),
        default=Value(None), output_field=DecimalField(max_digits=5, decimal_places=2))).select_related(
        'candidate__user', 'score__current_revision', 'current_assessment').order_by(F('_rank_score').desc(nulls_last=True), 'created_at', 'pk')
