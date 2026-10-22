from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .feedback import candidate_state, release
from .models import FeedbackReport, Interview
from .permissions import IsCandidate, IsRecruiter
from .schema_contracts import FeedbackListDTO, FeedbackEnvelopeDTO, FeedbackReviewDTO, FeedbackReviewListDTO


class StandardResultsPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


def private(data):
    response = Response(data)
    response['Cache-Control'] = 'no-store'
    return response


def candidate_feedback_interviews(user):
    """Scheduled/live and unverified legacy calls do not have feedback yet."""
    return Interview.objects.filter(
        application__candidate__user=user,
        status=Interview.Status.DONE,
        completed_at__isnull=False,
        completion_outcome__in=('USABLE', 'FAILED', 'INSUFFICIENT_EVIDENCE'),
    )


class CandidateFeedbackListView(APIView):
    permission_classes = [IsAuthenticated, IsCandidate]

    @extend_schema(tags=['Feedback'], operation_id='candidate_feedback_list', responses=FeedbackListDTO)
    def get(self, request):
        # The observed old screen ignores response content and renders sample grades
        # on ANY nonempty list. Require explicit contract opt-in until it is replaced.
        if request.query_params.get('contract') != 'feedback-v1':
            raise ValidationError({'contract': 'Integrate the feedback-v1 response and request contract=feedback-v1.'})
        if request.query_params.get('candidate', 'me') != 'me' or set(request.query_params) - {'contract', 'candidate', 'page', 'page_size'}:
            raise ValidationError('Use candidate=me; unsupported feedback query parameters.')
        interviews = candidate_feedback_interviews(request.user).order_by('-created_at', '-pk')
        paginator = StandardResultsPagination()
        page = paginator.paginate_queryset(interviews, request)
        response = paginator.get_paginated_response([candidate_state(row) for row in page])
        response['Cache-Control'] = 'no-store'
        return response


class CandidateFeedbackDetailView(APIView):
    permission_classes = [IsAuthenticated, IsCandidate]

    @extend_schema(tags=['Feedback'], responses=FeedbackEnvelopeDTO)
    def get(self, request, pk):
        row = get_object_or_404(FeedbackReport.objects.select_related('evaluation__interview'),
            pk=pk, evaluation__interview__in=candidate_feedback_interviews(request.user))
        return private(candidate_state(row.evaluation.interview, selected_report=row))


def review(row):
    return {'id': str(row.pk), 'revision': row.revision, 'contract_version': row.contract_version,
        'status': row.status, 'content_hash': row.content_hash, 'content': row.content if row.status == 'READY' else None,
        'evaluation': str(row.evaluation_id), 'prepared_at': row.prepared_at,
        'released_at': row.released_at, 'error_code': row.error_code,
        'edit_version': row.edit_version, 'amendments': row.amendments, 'retracted_at': row.retracted_at}


class FeedbackReviewView(APIView):
    permission_classes = [IsAuthenticated, IsRecruiter]

    @extend_schema(tags=['Feedback'], responses=FeedbackReviewListDTO)
    def get(self, request, pk):
        interview = get_object_or_404(Interview, pk=pk, application__job__recruiter__user=request.user)
        return private({'reports': [review(row) for row in FeedbackReport.objects.filter(
            evaluation__interview=interview).order_by('-revision')[:100]]})


class FeedbackReleaseView(APIView):
    permission_classes = [IsAuthenticated, IsRecruiter]

    class Input(serializers.Serializer):
        expected_revision = serializers.IntegerField(min_value=1)
        expected_edit_version = serializers.IntegerField(min_value=1, required=False)

        def to_internal_value(self, data):
            if not isinstance(data, dict) or set(data) - {'expected_revision', 'expected_edit_version'}:
                raise serializers.ValidationError({'non_field_errors': ['Supply documented revision fields only.']})
            return super().to_internal_value(data)

    @extend_schema(tags=['Feedback'], request=Input, responses=FeedbackReviewDTO)
    def post(self, request, pk):
        serializer = self.Input(data=request.data)
        serializer.is_valid(raise_exception=True)
        get_object_or_404(FeedbackReport, pk=pk, evaluation__interview__application__job__recruiter__user=request.user)
        row = release(pk, actor=request.user, **serializer.validated_data)
        return private(review(row))
