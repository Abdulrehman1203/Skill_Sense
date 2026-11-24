"""Raw-body HMAC verification before parsing provider tool requests."""
import json
from django.core.cache import cache
from drf_spectacular.utils import extend_schema, OpenApiResponse
from redis.exceptions import RedisError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from .conversation_control import control
from .integrations.retell_client import verify_webhook_signature
from .conversation_protocol import ANSWER_TOOL


def answer_resumed(payload):
    """Use newer authenticated speech already received, without waiting in the tool."""
    if payload.get('name') != ANSWER_TOOL:
        return False
    call = payload.get('call')
    if not isinstance(call, dict) or not isinstance(call.get('call_id'), str):
        return False
    from .live_transcript import transcript_key
    from .conversation_control import clean

    def candidate_text(turns):
        return ' '.join(clean(t.get('content', '')) for t in turns
                        if isinstance(t, dict) and t.get('role') == 'user').split()

    turns = call.get('transcript_object')
    if not isinstance(turns, list) or len(turns) > 10000:
        return False
    original = candidate_text(turns)
    if not original:
        return False
    try:
        latest = cache.get(transcript_key(call['call_id']))
    except RedisError:
        return False  # The signed tool payload remains valid evidence.
    if not latest:
        return False
    heard = candidate_text(latest['turns'])
    return len(heard) > len(original) and heard[:len(original)] == original


class ConversationControlView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = []

    def get(self, request):
        # Fixed, read-only capability probe. Never exposes call or candidate data.
        from .interview_readiness import CONTROL_PROTOCOL
        response = Response({'protocol': CONTROL_PROTOCOL})
        response['Cache-Control'] = 'no-store'
        return response

    @extend_schema(auth=[], request=None, responses={200: OpenApiResponse(description='Authenticated conversation instruction.'),
        401: OpenApiResponse(description='Invalid signature.'), 400: OpenApiResponse(description='Invalid command.')})
    def post(self, request):
        raw = request.body
        if len(raw) > 1_000_000:
            return Response({'detail': 'Payload too large.'}, status=413)
        if not verify_webhook_signature(raw, request.headers.get('x-retell-signature', '')):
            return Response({'detail': 'Invalid signature.'}, status=401)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return Response({'detail': 'Invalid JSON.'}, status=400)
        if not isinstance(payload, dict):
            return Response({'detail': 'Invalid payload.'}, status=400)
        if answer_resumed(payload):
            response = Response({
                'speech_action': 'WAIT', 'speak_text': '',
                'instruction': 'The candidate continued speaking. Listen to the full answer, then submit it again after they finish.',
            })
        else:
            response = Response(control(payload))
        response['Cache-Control'] = 'no-store'
        return response
