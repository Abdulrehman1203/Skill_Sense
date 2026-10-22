"""Owner-only explicit recovery operations; no client-authored evidence or scores."""
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import workflow_actions as service
from .models import Application, Interview, WorkflowAction
from .permissions import IsRecruiter
from .serializers import ApplicationDetailSerializer, InterviewSerializer
from .evaluation_views import report
from .feedback_views import review
from .schema_contracts import EvaluationDTO, TranscriptSelectionDTO, FeedbackReviewDTO, WorkflowHistoryDTO


class Command(serializers.Serializer):
    expected_version = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(max_length=2000, allow_blank=False)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError('Supply only documented command fields.')
        return super().to_internal_value(data)


class RerunCommand(Command):
    expected_revision = serializers.IntegerField(min_value=1)


class TranscriptCommand(Command):
    expected_selection_version = serializers.IntegerField(min_value=1)
    transcript_id = serializers.UUIDField()


class ScheduleCommand(Command):
    scheduled_at = serializers.DateTimeField()


class ReopenCommand(ScheduleCommand):
    resume_remaining = serializers.BooleanField(required=False)


class FeedbackCommand(Command):
    expected_edit_version = serializers.IntegerField(min_value=1)


class PublicGuidance(serializers.Serializer):
    summary = serializers.CharField(max_length=2000, required=False, allow_blank=False)
    improvement_suggestions = serializers.ListField(child=serializers.CharField(max_length=500), max_length=10, required=False)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or not data or set(data) - set(self.fields):
            raise serializers.ValidationError('Supply only candidate-facing summary or practice suggestions.')
        return super().to_internal_value(data)


class FeedbackEditCommand(FeedbackCommand):
    amendments = PublicGuidance()
    public_acknowledged = serializers.BooleanField()

    def validate_public_acknowledged(self, value):
        if value is not True:
            raise serializers.ValidationError('Explicitly acknowledge that this text is intended for the candidate.')
        return value


class PrivateWorkflowView(APIView):
    permission_classes = [IsAuthenticated, IsRecruiter]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        return response

    def command(self, request, cls):
        data = cls(data=request.data)
        data.is_valid(raise_exception=True)
        return data.validated_data


class EvaluationRerunView(PrivateWorkflowView):
    @extend_schema(request=RerunCommand, responses={202: EvaluationDTO})
    def post(self, request, pk):
        row = service.rerun(pk, request.user, self.command(request, RerunCommand))
        return Response(report(row, private=True), status=202)


class TranscriptSelectionView(PrivateWorkflowView):
    @extend_schema(request=TranscriptCommand, responses=TranscriptSelectionDTO)
    def post(self, request, pk):
        row = service.select_transcript(pk, request.user, self.command(request, TranscriptCommand))
        return Response({'selected_revision': row.selected_id, 'selection_version': row.selection_version, 'state': row.state})


class InterviewRescheduleView(PrivateWorkflowView):
    @extend_schema(request=ScheduleCommand, responses={200: InterviewSerializer})
    def post(self, request, pk):
        row = service.reschedule(pk, request.user, self.command(request, ScheduleCommand))
        return Response(InterviewSerializer(row).data)


class InterviewReopenView(PrivateWorkflowView):
    @extend_schema(request=ReopenCommand, responses={201: InterviewSerializer})
    def post(self, request, pk):
        row = service.reschedule(pk, request.user, self.command(request, ReopenCommand), reopen=True)
        return Response(InterviewSerializer(row).data, status=201)


class DecisionReversalView(PrivateWorkflowView):
    @extend_schema(request=Command, responses={200: ApplicationDetailSerializer})
    def post(self, request, pk):
        row = service.reverse_decision(pk, request.user, self.command(request, Command))
        return Response(ApplicationDetailSerializer(row, context={'request': request}).data)


class FeedbackEditView(PrivateWorkflowView):
    @extend_schema(request=FeedbackEditCommand, responses=FeedbackReviewDTO)
    def post(self, request, pk):
        return Response(review(service.change_feedback(pk, request.user, self.command(request, FeedbackEditCommand))))


class FeedbackRetractionView(PrivateWorkflowView):
    @extend_schema(request=FeedbackCommand, responses=FeedbackReviewDTO)
    def post(self, request, pk):
        return Response(review(service.change_feedback(pk, request.user, self.command(request, FeedbackCommand), retract=True)))


class WorkflowHistoryView(PrivateWorkflowView):
    @extend_schema(responses=WorkflowHistoryDTO)
    def get(self, request, pk):
        app = get_object_or_404(Application, pk=pk, job__recruiter__user=request.user)
        return Response({'actions': list(app.workflow_actions.order_by('-created_at', '-pk').values(
            'id', 'interview_id', 'actor_id', 'action', 'expected_version', 'reason', 'details', 'created_at')[:100])})
