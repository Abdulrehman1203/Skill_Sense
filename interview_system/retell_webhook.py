"""Verify raw bytes before parsing; enqueue only authenticated, validated events."""
import json
import logging

from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .integrations.retell_client import verify_webhook_signature
from .voice import accept_event, normalize_call

logger = logging.getLogger(__name__)


class RetellTranscriptSerializer(serializers.Serializer):
    retell_session_id = serializers.CharField(max_length=255)
    transcript = serializers.CharField(allow_blank=True, trim_whitespace=False)
    ended_at = serializers.DateTimeField()

    def to_internal_value(self, data):
        if not isinstance(data, dict) or any(
            not isinstance(data.get(key), str)
            for key in ('retell_session_id', 'transcript', 'ended_at')
        ):
            raise serializers.ValidationError({'non_field_errors': ['Session ID, transcript and ended_at must be strings.']})
        return super().to_internal_value(data)


class RetellCallEndedView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []  # HMAC only; do not run JWT authentication.
    parser_classes = []  # No DRF body parser can run before verification.

    @extend_schema(
        request=RetellTranscriptSerializer, auth=[],
        responses={204: OpenApiResponse(description='Verified unused lifecycle event acknowledged.'),
                   202: OpenApiResponse(description='Verified event durably saved for processing.'),
                   401: OpenApiResponse(description='Invalid or expired signature.'),
                   400: OpenApiResponse(description='Invalid verified payload.'),
                   500: OpenApiResponse(description='Event could not be persisted; sender should retry.')},
    )
    def post(self, request):
        raw = request.body  # Raw bytes are required for HMAC, never parsed here.
        if not verify_webhook_signature(raw, request.headers.get('x-retell-signature', '')):
            logger.warning(
                'Retell webhook rejected source_ip=%s timestamp=%s reason=invalid_signature',
                request.META.get('REMOTE_ADDR', 'unknown'), timezone.now().isoformat(),
            )
            return Response({'detail': 'Invalid webhook signature.'}, status=401)
        # SECURITY BOUNDARY: JSON and all field access are strictly below verification.
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return Response({'detail': 'Invalid JSON.'}, status=400)
        # Accept the documented application contract and Retell's native envelope.
        if isinstance(payload, dict) and 'event' in payload:
            # Retell's dashboard test sends call_started regardless of the
            # configured event filter. These events need no transcript work.
            if payload.get('event') == 'call_started':
                return Response(status=204)
            if payload.get('event') not in ('call_ended', 'call_analyzed'):
                return Response({'detail': 'Unsupported event.'}, status=400)
            event = payload['event']
            payload = normalize_call(payload.get('call'))
        else:
            event = 'call_ended'
        serializer = RetellTranscriptSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        extras = {key: payload[key] for key in ('status', 'disconnection_reason', 'transcript_object', 'recording_url', 'analysis') if key in payload}
        payload = {**serializer.validated_data, **extras}
        payload['ended_at'] = payload['ended_at'].isoformat()
        accept_event(payload, event)
        return Response({'status': 'accepted'}, status=202)
