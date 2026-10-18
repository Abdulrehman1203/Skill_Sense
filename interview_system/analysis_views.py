"""Clerk-only B6 reads: candidate-safe status and owner-recruiter reports."""
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework import serializers
from .socket_ticket_views import SocketTicketView
from .analysis_reporting import report_response, status_response


class AnalysisStatusSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    video_state = serializers.CharField()
    connection_state = serializers.CharField()
    analysis_state = serializers.CharField()
    revision = serializers.IntegerField()
    report_ready = serializers.BooleanField()
    final = serializers.BooleanField()
    video_required = serializers.BooleanField()
    availability = serializers.ChoiceField(choices=['disabled', 'legacy_unverified', 'pending', 'ready'])
    coverage = serializers.JSONField(required=False, allow_null=True)


class ReportEnvelopeSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    availability = serializers.ChoiceField(choices=['disabled', 'legacy_unverified', 'pending', 'ready'])
    revision = serializers.IntegerField()
    report = serializers.JSONField(allow_null=True)


class SnapshotEnvelopeSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    availability = serializers.ChoiceField(choices=['disabled', 'legacy_unverified', 'pending', 'ready'])
    revision = serializers.IntegerField()
    snapshot = serializers.JSONField(allow_null=True)


class AnalysisStatusView(SocketTicketView):
    http_method_names = ['get', 'head', 'options']

    @extend_schema(responses={200: AnalysisStatusSerializer})
    def get(self, request, pk):
        return Response(status_response(pk, request.user.pk))


class BehavioralReportView(SocketTicketView):
    http_method_names = ['get', 'head', 'options']

    @extend_schema(responses={200: ReportEnvelopeSerializer})
    def get(self, request, pk):
        return Response(report_response(pk, request.user.pk))


class AnalysisSnapshotView(SocketTicketView):
    http_method_names = ['get', 'head', 'options']

    @extend_schema(responses={200: SnapshotEnvelopeSerializer})
    def get(self, request, pk):
        return Response(report_response(pk, request.user.pk, snapshot=True))
