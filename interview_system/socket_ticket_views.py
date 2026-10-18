"""Clerk-authenticated opaque socket-ticket issuance; no downstream consumers yet."""
from drf_spectacular.utils import extend_schema
from django.db import DatabaseError
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import ClerkJWTAuthentication
from .socket_auth import PURPOSES, SocketAuthorizationError, issue_ticket


class TicketRequestSerializer(serializers.Serializer):
    purpose = serializers.ChoiceField(choices=PURPOSES)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) != {'purpose'}:
            raise serializers.ValidationError({'non_field_errors': ['Invalid request.']})
        return super().to_internal_value(data)


class TicketResponseSerializer(serializers.Serializer):
    ticket = serializers.CharField()
    expires_at = serializers.DateTimeField()
    socket_path = serializers.CharField()
    protocol_version = serializers.IntegerField()


class TicketErrorSerializer(serializers.Serializer):
    error = serializers.ChoiceField(choices=[
        'invalid_request', 'authentication_failed', 'credential_invalid', 'credential_expired',
        'access_denied', 'account_inactive', 'video_not_enabled', 'session_not_publishable',
        'ticket_limit', 'authorization_unavailable', 'method_not_allowed', 'not_acceptable',
        'unsupported_media_type', 'request_failed',
    ])


class SocketTicketView(APIView):
    authentication_classes = [ClerkJWTAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        response['Pragma'] = 'no-cache'
        return response

    def handle_exception(self, exc):
        # Stable errors, including authentication/parser/permission failures; no reflected secrets.
        if isinstance(exc, SocketAuthorizationError):
            return Response({'error': exc.reason}, status=exc.status)
        if isinstance(exc, DatabaseError):
            return Response({'error': 'authorization_unavailable'}, status=503)
        response = super().handle_exception(exc)
        reason = {400: 'invalid_request', 401: 'authentication_failed', 403: 'access_denied',
                  405: 'method_not_allowed', 406: 'not_acceptable', 415: 'unsupported_media_type',
                  429: 'ticket_limit'}.get(response.status_code, 'request_failed')
        response.data = {'error': reason}
        return response

    @extend_schema(request=TicketRequestSerializer, responses={
        200: TicketResponseSerializer, 400: TicketErrorSerializer, 401: TicketErrorSerializer,
        403: TicketErrorSerializer, 409: TicketErrorSerializer, 429: TicketErrorSerializer,
        503: TicketErrorSerializer,
    })
    def post(self, request, pk):
        options = TicketRequestSerializer(data=request.data)
        options.is_valid(raise_exception=True)
        result = issue_ticket(request.user.pk, pk, options.validated_data['purpose'], request.auth)
        return Response(TicketResponseSerializer(result).data)
