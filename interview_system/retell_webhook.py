"""Verify raw bytes before parsing; enqueue only authenticated, validated events."""
import json
import logging
import time
from datetime import datetime, timezone as datetime_timezone

from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiResponse
from kombu.exceptions import OperationalError as BrokerError
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .integrations.retell_client import verify_webhook_signature
from .tasks.interview_tasks import process_retell_transcript

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
        responses={202: OpenApiResponse(description='Verified event queued.'),
                   401: OpenApiResponse(description='Invalid or expired signature.'),
                   400: OpenApiResponse(description='Invalid verified payload.'),
                   503: OpenApiResponse(description='Queue unavailable; sender should retry.')},
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
            if payload.get('event') != 'call_ended':
                return Response({'detail': 'Unsupported event.'}, status=400)
            call = payload.get('call')
            if not isinstance(call, dict):
                return Response({'detail': 'Invalid call.'}, status=400)
            timestamp = call.get('end_timestamp')
            if type(timestamp) not in (int, float):
                return Response({'detail': 'Invalid end timestamp.'}, status=400)
            try:
                ended = datetime.fromtimestamp(timestamp / 1000, datetime_timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                return Response({'detail': 'Invalid end timestamp.'}, status=400)
            payload = {'retell_session_id': call.get('call_id'),
                       'transcript': call.get('transcript'), 'ended_at': ended}
        serializer = RetellTranscriptSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        payload = dict(serializer.validated_data)
        payload['ended_at'] = payload['ended_at'].isoformat()
        try:
            process_retell_transcript.apply_async(
                args=[payload], expires=19,
                headers={'retell_deadline': time.time() + 19},
                argsrepr='(<redacted Retell transcript>,)',
                retry=False,  # Return 503 promptly; provider delivery can retry.
            )
        except (BrokerError, OSError):
            logger.error('Verified Retell webhook could not be enqueued.')
            return Response({'detail': 'Queue unavailable.'}, status=503)
        return Response({'status': 'queued'}, status=202)
