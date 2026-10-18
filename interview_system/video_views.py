"""Candidate-owned idempotent sealing. Socket/analysis endpoints remain B3/B6."""
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from .socket_ticket_views import SocketTicketView, TicketErrorSerializer
from .video_lifecycle import finish_video


class FinishVideoRequestSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict) or data:
            raise serializers.ValidationError({'non_field_errors': ['Expected an empty object.']})
        return {}


class FinishVideoResponseSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    video_state = serializers.ChoiceField(choices=['finalizing', 'ended'])
    finish_reason = serializers.CharField()


class FinishVideoView(SocketTicketView):
    # Inherits Clerk-only auth, fixed handled errors, JSON parser and no-store.
    @extend_schema(request=FinishVideoRequestSerializer, responses={200: FinishVideoResponseSerializer,
        400: TicketErrorSerializer, 401: TicketErrorSerializer, 403: TicketErrorSerializer,
        409: TicketErrorSerializer, 503: TicketErrorSerializer})
    def post(self, request, pk):
        options = FinishVideoRequestSerializer(data=request.data)
        options.is_valid(raise_exception=True)
        return Response(FinishVideoResponseSerializer(finish_video(pk, request.user.pk)).data)
