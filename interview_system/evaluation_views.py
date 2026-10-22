"""Role-specific evaluation history and explicit frozen feedback release."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import EvaluationRelease, Interview, InterviewEvaluation
from .permissions import IsCandidate, IsRecruiter
from .screening import ScreeningConflict
from .schema_contracts import EvaluationDTO, EvaluationHistoryDTO, TranscriptHistoryDTO, FeedbackEnvelopeDTO


def report(row, *, private):
    if not private:
        raise ValueError('Candidate feedback must use the frozen FeedbackReport projection.')
    result = {'id': str(row.pk), 'revision': row.revision, 'status': row.status,
        'supersedes': str(row.supersedes_id) if row.supersedes_id else None,
        'score': row.score, 'coverage': row.coverage, 'published_at': row.published_at,
        'answers': list(row.answers.order_by('pk').values_list('result', flat=True))}
    if private:
        result.update(transcript_revision=row.transcript.number, transcript_hash=row.transcript.content_hash,
            context_hash=row.context.content_hash, contract_version=row.prompt_version, model_name=row.model_name,
            error_code=row.error_code, attempts=row.attempts, next_attempt_at=row.next_attempt_at,
            feedback_released=EvaluationRelease.objects.filter(evaluation=row).exists())
        extraction = getattr(row, 'extraction', None)
        result['extraction'] = extraction.content if extraction else None
        result['run_number'] = row.run_number
    return result


class EvaluationHistoryView(APIView):
    permission_classes = [IsAuthenticated, IsRecruiter]

    @extend_schema(tags=['Interview evaluations'], responses=EvaluationHistoryDTO)
    def get(self, request, pk):
        interview = get_object_or_404(Interview, pk=pk, application__job__recruiter__user=request.user)
        prep = getattr(interview, 'transcript_preparation', None)
        response = Response({'transcript': {'state': prep.state if prep else 'NOT_STARTED',
            'selected_source': prep.selected_source if prep else '',
            'reason': prep.reason if prep else '', 'deadline': prep.deadline if prep else None},
            'evaluations': [report(row, private=True) for row in interview.evaluations.select_related('transcript', 'context').order_by('-revision')[:100]]})
        response['Cache-Control'] = 'no-store'
        return response


class TranscriptHistoryView(APIView):
    permission_classes = [IsAuthenticated, IsRecruiter]

    @extend_schema(tags=['Interview evaluations'], responses=TranscriptHistoryDTO)
    def get(self, request, pk):
        interview = get_object_or_404(Interview, pk=pk, application__job__recruiter__user=request.user)
        prep = getattr(interview, 'transcript_preparation', None)
        response = Response({'selected_revision': prep.selected_id if prep else None,
            'selection_version': prep.selection_version if prep else None,
            'selection_pinned': bool(prep and prep.selected_by_id),
            'revisions': list(interview.transcript_revisions.order_by('-number').values(
                'id', 'number', 'call_id', 'source', 'content_hash', 'text', 'turns', 'created_at')[:100])})
        response['Cache-Control'] = 'no-store'
        return response


class EvaluationReleaseView(APIView):
    permission_classes = [IsAuthenticated, IsRecruiter]

    @transaction.atomic
    @extend_schema(tags=['Interview evaluations'], request=None, responses=EvaluationDTO)
    def post(self, request, pk):
        if request.data:
            raise ValidationError('Release accepts no writable score or evidence fields.')
        ref = get_object_or_404(InterviewEvaluation, pk=pk, interview__application__job__recruiter__user=request.user)
        interview = Interview.objects.select_for_update().get(pk=ref.interview_id)
        row = InterviewEvaluation.objects.select_related('context', 'transcript').get(pk=pk)
        if interview.status != 'DONE' or row.status != 'READY':
            raise ValidationError('Only completed validated feedback can be released.')
        from .feedback import prepare_locked, release
        projection = prepare_locked(row)
        if projection.edit_version != 1:
            raise ValidationError('Use the feedback release endpoint to confirm the previewed public-guidance version.')
        release(projection.pk, actor=request.user, expected_revision=projection.revision)
        response = Response(report(row, private=True))
        response['Cache-Control'] = 'no-store'
        return response


class EvaluationFeedbackView(APIView):
    permission_classes = [IsAuthenticated, IsCandidate]

    @extend_schema(tags=['Interview evaluations'], responses=FeedbackEnvelopeDTO)
    def get(self, request, pk):
        interview = get_object_or_404(Interview, pk=pk, application__candidate__user=request.user)
        from .feedback import candidate_state
        response = Response(candidate_state(interview))
        response['Cache-Control'] = 'no-store'
        return response
