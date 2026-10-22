"""Explicit wire DTOs for the revisioned recruitment workflow.

JSONField is limited to explicitly extensible metadata (policy snapshots and audit
details); resource envelopes and ordinary response fields have concrete schemas.
"""
from rest_framework import serializers as s


class PreparedQuestionDTO(s.Serializer):
    id = s.UUIDField()
    text = s.CharField()
    category = s.ChoiceField(choices=['TECHNICAL', 'BEHAVIORAL', 'SITUATIONAL'])
    source = s.ChoiceField(choices=['GENERATED', 'TEMPLATE', 'MANUAL'])
    position = s.IntegerField(min_value=0)


class QuestionSetDTO(s.Serializer):
    id = s.UUIDField(required=False)
    application = s.UUIDField(required=False)
    assessment = s.UUIDField(allow_null=True, required=False)
    assessment_revision = s.IntegerField(allow_null=True, required=False)
    revision = s.IntegerField(required=False)
    input_hash = s.CharField(required=False)
    generation_status = s.ChoiceField(choices=['NOT_REQUESTED','QUEUED','GENERATING','READY','FALLBACK','FAILED','LEGACY'])
    generation_number = s.IntegerField(required=False)
    attempts = s.IntegerField(required=False)
    next_attempt_at = s.DateTimeField(allow_null=True, required=False)
    error_code = s.CharField(required=False, allow_blank=True)
    fallback_reason = s.CharField(required=False, allow_blank=True)
    approved_at = s.DateTimeField(allow_null=True, required=False)
    frozen_at = s.DateTimeField(allow_null=True, required=False)
    frozen_revision = s.IntegerField(allow_null=True, required=False)
    legacy = s.BooleanField(required=False)
    questions = PreparedQuestionDTO(many=True)


class TurnDTO(s.Serializer):
    id = s.CharField()
    role = s.ChoiceField(choices=['agent','user'])
    text = s.CharField()
    start_seconds = s.FloatField(required=False)
    end_seconds = s.FloatField(required=False)


class TranscriptDTO(s.Serializer):
    id = s.UUIDField()
    number = s.IntegerField()
    call_id = s.CharField()
    source = s.CharField()
    content_hash = s.CharField()
    text = s.CharField(allow_blank=True)
    turns = TurnDTO(many=True)
    created_at = s.DateTimeField()


class TranscriptHistoryDTO(s.Serializer):
    selected_revision = s.UUIDField(allow_null=True)
    selection_version = s.IntegerField(allow_null=True)
    selection_pinned = s.BooleanField()
    revisions = TranscriptDTO(many=True)


class TranscriptSelectionDTO(s.Serializer):
    selected_revision = s.UUIDField(allow_null=True)
    selection_version = s.IntegerField()
    state = s.ChoiceField(choices=['WAITING','READY','UNAVAILABLE'])


class EvidenceDTO(s.Serializer):
    turn_id = s.CharField()
    quote = s.CharField()


class AnswerDTO(s.Serializer):
    question_id = s.UUIDField()
    status = s.ChoiceField(choices=['ANSWERED','NO_ANSWER','MISSING_EVIDENCE'])
    criteria = s.DictField(child=s.FloatField(min_value=0, max_value=1, allow_null=True))
    strengths = s.ListField(child=s.CharField())
    gaps = s.ListField(child=s.CharField())
    rationale = s.CharField()
    evidence = EvidenceDTO(many=True)
    uncertainty = s.FloatField(min_value=0, max_value=1)


class SpanDTO(s.Serializer):
    turn_id = s.CharField()
    start_offset = s.IntegerField()
    end_offset = s.IntegerField()
    start_seconds = s.FloatField(allow_null=True)
    end_seconds = s.FloatField(allow_null=True)


class AttributedAnswerDTO(s.Serializer):
    question_id = s.UUIDField()
    state = s.ChoiceField(choices=['ANSWERED','NO_ANSWER','NOT_ASKED','PARTIAL','UNALIGNABLE'])
    confidence = s.FloatField(min_value=0, max_value=1)
    method = s.ChoiceField(choices=['EXACT','LEXICAL','SEMANTIC','NONE'])
    asked_turn_id = s.CharField(allow_null=True)
    answer_turn_ids = s.ListField(child=s.CharField())
    follow_up_turn_ids = s.ListField(child=s.CharField())
    spans = SpanDTO(many=True)


class ExtractionDTO(s.Serializer):
    questions = AttributedAnswerDTO(many=True)
    unmatched_turn_ids = s.ListField(child=s.CharField())


class EvaluationDTO(s.Serializer):
    id = s.UUIDField()
    revision = s.IntegerField()
    status = s.ChoiceField(choices=['QUEUED','PROCESSING','READY','FAILED','UNAVAILABLE'])
    supersedes = s.UUIDField(allow_null=True)
    score = s.FloatField(allow_null=True)
    coverage = s.FloatField(allow_null=True)
    published_at = s.DateTimeField(allow_null=True)
    answers = AnswerDTO(many=True)
    transcript_revision = s.IntegerField()
    transcript_hash = s.CharField()
    context_hash = s.CharField()
    contract_version = s.CharField()
    model_name = s.CharField()
    error_code = s.CharField(allow_blank=True)
    attempts = s.IntegerField()
    next_attempt_at = s.DateTimeField(allow_null=True)
    feedback_released = s.BooleanField()
    extraction = ExtractionDTO(allow_null=True)
    run_number = s.IntegerField()


class TranscriptStateDTO(s.Serializer):
    state = s.ChoiceField(choices=['NOT_STARTED','WAITING','READY','UNAVAILABLE'])
    selected_source = s.CharField(allow_blank=True)
    reason = s.CharField(allow_blank=True)
    deadline = s.DateTimeField(allow_null=True)


class EvaluationHistoryDTO(s.Serializer):
    transcript = TranscriptStateDTO()
    evaluations = EvaluationDTO(many=True)


class InterviewSummaryDTO(s.Serializer):
    text = s.CharField()
    performance_percentage = s.FloatField(allow_null=True)
    answer_coverage = s.FloatField()
    assessed_questions = s.IntegerField()
    total_questions = s.IntegerField()


class DimensionDTO(s.Serializer):
    id = s.CharField()
    label = s.CharField()
    percentage = s.FloatField()
    assessed_answers = s.IntegerField()


class FindingDTO(s.Serializer):
    dimension_id = s.CharField()
    description = s.CharField()


class SuggestionDTO(s.Serializer):
    dimension_id = s.CharField()
    action = s.CharField()


class PublicGuidanceDTO(s.Serializer):
    summary = s.CharField(required=False)
    improvement_suggestions = s.ListField(child=s.CharField(), required=False)


class FeedbackContentDTO(s.Serializer):
    interview_summary = InterviewSummaryDTO()
    performance_dimensions = DimensionDTO(many=True)
    strengths = FindingDTO(many=True)
    gaps = FindingDTO(many=True)
    improvement_suggestions = SuggestionDTO(many=True)
    limitations = s.ListField(child=s.CharField())


class ReleasedFeedbackDTO(FeedbackContentDTO):
    id = s.UUIDField()
    revision = s.IntegerField()
    contract_version = s.CharField()
    released_at = s.DateTimeField()
    recruiter_public_guidance = PublicGuidanceDTO(required=False)


class FeedbackEnvelopeDTO(s.Serializer):
    state = s.ChoiceField(choices=['PENDING','FAILED','UNAVAILABLE','UNRELEASED','RELEASED','RETRACTED'])
    interview = s.UUIDField()
    application = s.UUIDField()
    report = ReleasedFeedbackDTO(allow_null=True)


class FeedbackListDTO(s.Serializer):
    count = s.IntegerField()
    next = s.URLField(allow_null=True)
    previous = s.URLField(allow_null=True)
    results = FeedbackEnvelopeDTO(many=True)


class FeedbackReviewDTO(s.Serializer):
    id = s.UUIDField()
    revision = s.IntegerField()
    contract_version = s.CharField()
    status = s.ChoiceField(choices=['PENDING','READY','FAILED','UNAVAILABLE'])
    content_hash = s.CharField(allow_blank=True)
    content = FeedbackContentDTO(allow_null=True)
    evaluation = s.UUIDField()
    prepared_at = s.DateTimeField(allow_null=True)
    released_at = s.DateTimeField(allow_null=True)
    error_code = s.CharField(allow_blank=True)
    edit_version = s.IntegerField()
    amendments = PublicGuidanceDTO()
    retracted_at = s.DateTimeField(allow_null=True)


class FeedbackReviewListDTO(s.Serializer):
    reports = FeedbackReviewDTO(many=True)


class ScoreHistoryDTO(s.Serializer):
    id = s.UUIDField()
    number = s.IntegerField()
    status = s.CharField()
    inputs = s.JSONField(help_text='Immutable versioned scoring-policy input snapshot; not frontend editable.')
    final_score = s.FloatField(allow_null=True)
    screening_match_percentage = s.FloatField(allow_null=True)
    interview_performance_percentage = s.FloatField(allow_null=True)
    coverage = s.FloatField(allow_null=True)
    error_code = s.CharField(allow_blank=True)
    attempts = s.IntegerField()
    created_at = s.DateTimeField()
    completed_at = s.DateTimeField(allow_null=True)


class ScoringHistoryDTO(s.Serializer):
    from .serializers import CandidateScoreResponseSerializer
    current = CandidateScoreResponseSerializer()
    history = ScoreHistoryDTO(many=True)


class WorkflowActionDTO(s.Serializer):
    id = s.UUIDField()
    interview_id = s.UUIDField(allow_null=True)
    actor_id = s.UUIDField()
    action = s.CharField()
    expected_version = s.IntegerField()
    reason = s.CharField()
    details = s.JSONField(help_text='Versioned action-specific private audit details; not candidate content.')
    created_at = s.DateTimeField()


class WorkflowHistoryDTO(s.Serializer):
    actions = WorkflowActionDTO(many=True)
