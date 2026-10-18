"""Authenticated HTTP lifecycle for browser voice interviews."""
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from .integrations import retell_client
from .models import Interview, InterviewSession, RetellCall, User
from .serializers import StartVoiceSessionResponseSerializer
from .voice import accept_event, normalize_call


class VoiceStartSerializer(serializers.Serializer):
    video_required = serializers.BooleanField(default=False)
    start_now = serializers.BooleanField(default=False)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - {'video_required', 'start_now'}:
            raise ValidationError('Only video_required and start_now may be supplied.')
        return super().to_internal_value(data)


class VoiceResultSerializer(serializers.Serializer):
    interview_session_id = serializers.UUIDField(allow_null=True)
    status = serializers.CharField()
    retell_session_id = serializers.CharField(allow_null=True)
    started_at = serializers.DateTimeField(allow_null=True)
    ended_at = serializers.DateTimeField(allow_null=True)
    audio_ended_at = serializers.DateTimeField(allow_null=True)
    transcript = serializers.CharField(allow_blank=True)
    transcript_object = serializers.ListField()
    recording_url = serializers.CharField(allow_blank=True)
    disconnection_reason = serializers.CharField(allow_blank=True)
    call_analysis = serializers.DictField()


def _private(response):
    response['Cache-Control'] = 'no-store'
    response['Pragma'] = 'no-cache'
    return response


class VoiceInterviewMixin:
    @extend_schema(tags=['Interviews'], request=VoiceStartSerializer,
                   responses={200: StartVoiceSessionResponseSerializer, 201: StartVoiceSessionResponseSerializer,
                              409: OpenApiResponse(description='Call completed or credentials expired.'),
                              503: OpenApiResponse(description='Voice provider unavailable; retry the same endpoint.')})
    @action(detail=True, methods=['post'], url_path='start-voice-session')
    def start_voice_session(self, request, pk=None):
        options = VoiceStartSerializer(data=request.data)
        options.is_valid(raise_exception=True)
        with transaction.atomic():
            interview = get_object_or_404(Interview.objects.select_for_update(), pk=pk)
            app = interview.application
            if request.user.pk not in (app.candidate.user_id, app.job.recruiter.user_id):
                raise PermissionDenied('You do not have access to this interview.')
            # Explicit local testing action only. Never relax production joining
            # windows, ownership, or completed-call rules. Local template
            # scaffolding below is confined to this explicit development action.
            start_now = options.validated_data['start_now']
            if start_now and not settings.DEBUG:
                raise PermissionDenied('Immediate test interviews are available only in development.')
            if interview.status == Interview.Status.DONE:
                return Response({'detail': 'This interview is complete.'}, status=409)
            if interview.status not in (Interview.Status.SCHEDULED, Interview.Status.LIVE):
                return Response({'detail': 'This interview has an invalid lifecycle state.'}, status=409)
            if (not start_now and request.user.role == User.Role.CANDIDATE and interview.status == Interview.Status.SCHEDULED
                    and interview.scheduled_at and interview.scheduled_at > timezone.now() + timedelta(minutes=15)):
                raise ValidationError({'scheduled_at': 'You can join from 15 minutes before the scheduled time.'})
            questions = list(interview.questions.filter(approved=True).order_by('created_at', 'pk'))
            if not questions and not start_now:
                raise ValidationError({'questions': 'At least one approved question is required.'})
            session, created = InterviewSession.objects.get_or_create(
                interview=interview, defaults={'video_required': options.validated_data['video_required']},
            )
            if session.audio_ended_at is not None or session.ended_at is not None:
                return Response({'detail': 'This session has ended.'}, status=409)
            if session.creation_uncertain:
                return Response({'detail': 'Previous call creation is being reconciled. Use sync-voice-session before retrying.'}, status=409)
            if not created and 'video_required' in request.data and session.video_required != options.validated_data['video_required']:
                raise ValidationError({'video_required': 'Session mode cannot change after creation.'})
            if not questions:
                from .development_interviews import prepare_development_questions_locked
                questions = prepare_development_questions_locked(interview, request.user)
                if not questions:
                    raise ValidationError({'questions': 'At least one approved question is required.'})
            call = RetellCall.objects.filter(call_id=interview.retell_session_id).first()
            try:
                if call:
                    if call.ended_at is not None:
                        return Response({'detail': 'This call has ended; results are processing.'}, status=409)
                    if call.expires_at and call.expires_at > int(time.time() * 1000) and call.credentials:
                        credentials = retell_client.decrypt_credentials(call.credentials)
                        return _private(Response({
                            'interview_session_id': session.pk, 'retell_session_id': call.call_id, **credentials,
                        }))
                    # Expired tokens must never silently create a second active call.
                    remote = retell_client.get_call(call.call_id)
                    if remote.get('call_status') == 'registered':
                        retell_client.stop_call(call.call_id)
                        call.status = 'superseded'
                        call.credentials = ''
                        call.ended_at = timezone.now()
                        call.save(update_fields=['status', 'credentials', 'ended_at'])
                    elif remote.get('call_status') == 'not_connected':
                        call.status = 'superseded'
                        call.credentials = ''
                        call.ended_at = timezone.now()
                        call.save(update_fields=['status', 'credentials', 'ended_at'])
                    else:
                        if remote.get('call_status') in ('ended', 'error'):
                            accept_event(normalize_call(remote))
                        return Response({'detail': 'Call is active or complete; credentials cannot be renewed.'}, status=409)
                    interview.retell_session_id = None
                    interview.save(update_fields=['retell_session_id', 'updated_at'])
                elif interview.retell_session_id:
                    # Older call-ID-only sessions cannot safely be replaced.
                    return Response({'detail': 'Legacy call has no join credentials. Use sync-voice-session to recover its state.'}, status=409)
                credentials = retell_client.create_web_call(interview, questions)
                call = RetellCall.objects.create(
                    call_id=credentials['call_id'], session=session,
                    credentials=retell_client.encrypt_credentials(credentials), expires_at=credentials['expires_at'],
                )
            except retell_client.RetellCreationUncertainError:
                session.creation_uncertain = True
                session.save(update_fields=['creation_uncertain'])
                return _private(Response({'detail': 'Call creation outcome is unknown. Use sync-voice-session before retrying.'}, status=503))
            except retell_client.RetellUnavailableError:
                # Leave SCHEDULED sessions retryable; no fake LIVE success.
                return _private(Response({'detail': 'Voice service unavailable. Retry this endpoint.'}, status=503))
            interview.retell_session_id = call.call_id
            interview.status = Interview.Status.LIVE
            interview.save(update_fields=['retell_session_id', 'status', 'updated_at'])
            if session.started_at is None:
                session.started_at = timezone.now()
                session.save(update_fields=['started_at'])
            from .video_lifecycle import initialize_video_locked
            initialize_video_locked(session)
            return _private(Response({
                'interview_session_id': session.pk, 'retell_session_id': call.call_id, **credentials,
            }, status=201))

    @extend_schema(tags=['Interviews'], responses={200: VoiceResultSerializer})
    @action(detail=True, methods=['get'], url_path='voice-session')
    def voice_session(self, request, pk=None):
        interview = self.get_object()
        session = InterviewSession.objects.filter(interview=interview).first()
        call = RetellCall.objects.filter(call_id=interview.retell_session_id).first()
        return _private(Response(VoiceResultSerializer({
            'interview_session_id': session.pk if session else None,
            'status': interview.status, 'retell_session_id': interview.retell_session_id,
            'started_at': session.started_at if session else None,
            'ended_at': session.ended_at if session else None,
            'audio_ended_at': session.audio_ended_at if session else None,
            'transcript': session.transcript if session else '',
            'transcript_object': call.transcript_object if call else [],
            'recording_url': call.recording_url if call else '',
            'disconnection_reason': call.disconnection_reason if call else '',
            'call_analysis': call.analysis if call else {},
        }).data))

    @extend_schema(tags=['Interviews'], request=None, responses={202: OpenApiResponse(description='Completion requested; await verified provider results.')})
    @action(detail=True, methods=['post'], url_path='end-voice-session')
    def end_voice_session(self, request, pk=None):
        interview = self.get_object()
        if interview.status == Interview.Status.DONE:
            return self.voice_session(request, pk)
        if not interview.retell_session_id:
            raise ValidationError('No voice call has started.')
        try:
            data = retell_client.get_call(interview.retell_session_id)
            if data.get('call_status') in ('ended', 'error', 'not_connected'):
                accept_event(normalize_call(data), 'call_analyzed' if data.get('call_analysis') else 'call_ended')
            else:
                retell_client.stop_call(interview.retell_session_id)
        except retell_client.RetellUnavailableError:
            return Response({'detail': 'Voice service unavailable; retry ending the call.'}, status=503)
        return Response({'detail': 'Call completion requested. Results will arrive through the webhook.'}, status=202)

    @extend_schema(tags=['Interviews'], request=None, responses={202: OpenApiResponse(description='Terminal results saved for processing.'),
                                                              200: OpenApiResponse(description='Call remains active.')})
    @action(detail=True, methods=['post'], url_path='sync-voice-session')
    def sync_voice_session(self, request, pk=None):
        interview = self.get_object()
        session = InterviewSession.objects.filter(interview=interview).first()
        if session and session.creation_uncertain:
            from .voice import reconcile_creation
            try:
                if not reconcile_creation(interview.pk):
                    return Response({'detail': 'Call creation is still being reconciled. Retry sync later.'}, status=409)
            except retell_client.RetellUnavailableError:
                return Response({'detail': 'Voice service unavailable.'}, status=503)
            interview.refresh_from_db()
        if not interview.retell_session_id:
            raise ValidationError('No voice call has started.')
        try:
            data = retell_client.get_call(interview.retell_session_id)
            if data.get('call_status') in ('ended', 'error', 'not_connected'):
                accept_event(normalize_call(data), 'call_analyzed' if data.get('call_analysis') else 'call_ended')
                return Response({'status': 'accepted'}, status=202)
        except retell_client.RetellUnavailableError:
            return Response({'detail': 'Voice service unavailable.'}, status=503)
        return Response({'status': data.get('call_status')})
