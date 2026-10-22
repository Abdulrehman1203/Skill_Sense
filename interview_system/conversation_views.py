"""Raw-body HMAC verification before parsing provider tool requests."""
import json
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from .conversation_control import control
from .integrations.retell_client import verify_webhook_signature


class ConversationControlView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = []

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
        response = Response(control(payload))
        response['Cache-Control'] = 'no-store'
        return response
