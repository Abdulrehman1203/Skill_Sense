import hashlib
import re
import time
from datetime import timedelta

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import Interview, InterviewReplay, InterviewSession, ReplayObject, RetellCall
from ..permissions import IsCandidate, IsRecruiter
from . import storage

State = InterviewReplay.State
CHUNK_LIMIT = 1800
MAX_CLOCK_UNCERTAINTY_MS = 180
UPLOAD_GRACE = timedelta(seconds=90)


def private(data=None, status=200):
    return Response(data, status=status, headers={'Cache-Control': 'private, no-store'})


def candidate_session(request, pk):
    return get_object_or_404(InterviewSession.objects.select_related('interview'), pk=pk,
                             interview__application__candidate__user=request.user)


class SegmentInput(serializers.Serializer):
    nonce = serializers.UUIDField()
    call_id = serializers.CharField(max_length=255)
    acknowledged = serializers.BooleanField()
    mime = serializers.ChoiceField(choices=['video/webm', 'video/mp4'])
    start_ms = serializers.IntegerField(min_value=0)
    uncertainty_ms = serializers.IntegerField(min_value=0, max_value=MAX_CLOCK_UNCERTAINTY_MS)


class ReplayConsent(APIView):
    permission_classes = [IsAuthenticated, IsCandidate]

    @extend_schema(responses=inline_serializer('ReplayCaptureInfo', fields={
        'enabled': serializers.BooleanField(), 'server_ms': serializers.IntegerField(),
    }))
    def get(self, request, pk):
        session = candidate_session(request, pk)
        return private({'enabled': settings.REPLAY_CAPTURE_ENABLED and storage.configured() and session.video_required,
                        'server_ms': time.time_ns() // 1_000_000})

    @extend_schema(request=SegmentInput, responses=inline_serializer('ReplaySegmentReceipt', fields={
        'segment': serializers.IntegerField(),
    }))
    def post(self, request, pk):
        session = candidate_session(request, pk)
        data = SegmentInput(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        if not values['acknowledged']:
            return private(status=400)
        now = timezone.now()
        with transaction.atomic():
            attempt = Interview.objects.select_for_update().get(pk=session.interview_id)
            call = get_object_or_404(RetellCall, pk=values['call_id'], session=session)
            replay = InterviewReplay.objects.select_for_update().filter(session=session).first()
            if replay and replay.call_id == call.pk:
                segment = replay.segments.filter(nonce=values['nonce']).first()
                if segment:
                    if (segment.mime != values['mime'] or segment.start_ms != values['start_ms']
                            or segment.uncertainty_ms != values['uncertainty_ms']):
                        return private(status=409)
                    return private({'segment': segment.pk})
            if (attempt.retell_session_id != call.pk or attempt.status != Interview.Status.LIVE
                    or call.ended_at is not None or not session.video_required
                    or abs(int(now.timestamp() * 1000) - values['start_ms']) > 15_000):
                return private(status=409)
            if replay is None:
                if not settings.REPLAY_CAPTURE_ENABLED or not storage.configured():
                    return private(status=409)
                capture_until = now + timedelta(seconds=storage.MAX_CAPTURE)
                replay = InterviewReplay.objects.create(
                    session=session, call=call, acknowledged_at=now, capture_until=capture_until,
                    finalize_until=capture_until + timedelta(minutes=25), due_at=now + UPLOAD_GRACE,
                    expires_at=now + timedelta(days=settings.REPLAY_RETENTION_DAYS),
                )
            if (replay.state != State.RECORDING or replay.call_id != call.pk or replay.capture_until <= now
                    or replay.segments.count() >= storage.MAX_SEGMENTS):
                return private(status=409)
            segment = replay.segments.create(nonce=values['nonce'], mime=values['mime'],
                start_ms=values['start_ms'], uncertainty_ms=values['uncertainty_ms'])
            replay.last_activity = now
            replay.save(update_fields=['last_activity'])
        return private({'segment': segment.pk}, 201)


def locked_segment(session, segment):
    replay = get_object_or_404(InterviewReplay.objects.select_for_update(), session=session)
    return replay, get_object_or_404(replay.segments, pk=segment)


class ReplayChunk(APIView):
    permission_classes = [IsAuthenticated, IsCandidate]
    parser_classes = []

    @extend_schema(request={'application/octet-stream': OpenApiTypes.BINARY}, parameters=[
        OpenApiParameter('X-Replay-Elapsed', int, OpenApiParameter.HEADER, required=True),
        OpenApiParameter('X-Replay-Sha256', str, OpenApiParameter.HEADER, required=True),
    ], responses=inline_serializer('ReplayChunkReceipt', fields={'sequence': serializers.IntegerField()}))
    def put(self, request, pk, segment, sequence):
        session = candidate_session(request, pk)
        declared = request.META.get('CONTENT_LENGTH', '')
        elapsed = request.headers.get('X-Replay-Elapsed', '')
        digest = request.headers.get('X-Replay-Sha256', '')
        if (not re.fullmatch(r'[0-9]{1,7}', declared) or not 0 < int(declared) <= storage.MAX_CHUNK
                or sequence >= CHUNK_LIMIT or not re.fullmatch(r'[0-9]{1,7}', elapsed)
                or int(elapsed) > storage.MAX_CAPTURE * 1000 or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            return private(status=400)
        body = request.body
        if len(body) != int(declared) or hashlib.sha256(body).hexdigest() != digest:
            return private(status=400)
        now = timezone.now()
        try:
            with transaction.atomic():
                replay, row = locked_segment(session, segment)
                key = f'{storage.PREFIX}{replay.pk}/segments/{row.pk}/{sequence:05d}'
                obj = ReplayObject.objects.filter(pk=key).first()
                if obj and (obj.digest != digest or obj.elapsed_ms != int(elapsed) or obj.size != len(body)):
                    return private(status=409)
                if obj and obj.verified:
                    return private({'sequence': sequence})
                if (replay.state != State.RECORDING or replay.capture_until + UPLOAD_GRACE <= now
                        or row.ended_ms is not None or sequence != row.next_sequence):
                    return private(status=409)
                if obj is None:
                    previous = row.chunks.filter(verified=True, sequence=sequence - 1).first()
                    if previous and previous.elapsed_ms > int(elapsed):
                        return private(status=409)
                    with storage.budget() as ledger:
                        used = ReplayObject.objects.filter(replay=replay).aggregate(total=Sum('size'))['total'] or 0
                        if used + len(body) > storage.MAX_INPUT:
                            raise storage.QuotaExceeded
                        storage.reserve(ledger, len(body))
                        ReplayObject.objects.create(key=key, replay=replay, segment=row, sequence=sequence,
                            size=len(body), digest=digest, elapsed_ms=int(elapsed), delete_after=replay.expires_at)
            # Reservation commits before the remote write. Holding the same recording
            # lock here fences both finalization and another request for this sequence.
            with transaction.atomic():
                replay, row = locked_segment(session, segment)
                obj = ReplayObject.objects.get(pk=key)
                if obj.verified:
                    return private({'sequence': sequence})
                if (replay.state != State.RECORDING or row.ended_ms is not None or row.next_sequence != sequence
                        or replay.capture_until + UPLOAD_GRACE <= timezone.now()):
                    return private(status=409)
                storage.put_chunk(storage.client(), obj, body)
                obj.verified = True
                obj.save(update_fields=['verified'])
                row.next_sequence += 1
                row.save(update_fields=['next_sequence'])
                replay.last_activity = timezone.now()
                replay.save(update_fields=['last_activity'])
        except storage.QuotaExceeded:
            return private(status=507)
        except (BotoCoreError, ClientError, ValueError):
            return private(status=503)
        return private({'sequence': sequence})


class SealInput(serializers.Serializer):
    elapsed_ms = serializers.IntegerField(min_value=0, max_value=storage.MAX_CAPTURE * 1000)
    count = serializers.IntegerField(min_value=0, max_value=CHUNK_LIMIT)
    complete = serializers.BooleanField()


class ReplaySeal(APIView):
    permission_classes = [IsAuthenticated, IsCandidate]

    @extend_schema(request=SealInput, responses={204: None})
    def post(self, request, pk, segment):
        session = candidate_session(request, pk)
        data = SealInput(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        with transaction.atomic():
            replay, row = locked_segment(session, segment)
            if row.ended_ms is not None:
                return private(status=204)
            latest = row.chunks.filter(verified=True).order_by('-sequence').first()
            if (replay.state != State.RECORDING or values['count'] != row.next_sequence
                    or (latest and latest.elapsed_ms > values['elapsed_ms'])):
                return private(status=409)
            row.ended_ms = values['elapsed_ms']
            row.complete = values['complete']
            row.save(update_fields=['ended_ms', 'complete'])
            replay.last_activity = timezone.now()
            replay.save(update_fields=['last_activity'])
        return private(status=204)


class ReplayPlayback(APIView):
    permission_classes = [IsAuthenticated, IsRecruiter]

    @extend_schema(responses=inline_serializer('InterviewReplayStatus', fields={
        'state': serializers.ChoiceField(choices=State.choices),
        'gaps': serializers.ListField(child=serializers.DictField(child=serializers.FloatField()), required=False),
        'url': serializers.URLField(required=False),
        'expires_in': serializers.IntegerField(required=False),
    }))
    def get(self, request, pk):
        attempt = get_object_or_404(Interview, pk=pk, application__job__recruiter__user=request.user)
        if not settings.REPLAY_PLAYBACK_ENABLED:
            return private({'state': State.UNAVAILABLE})
        replay = InterviewReplay.objects.filter(session__interview=attempt).first()
        if not replay:
            return private({'state': State.UNAVAILABLE})
        remaining = int((replay.expires_at - timezone.now()).total_seconds())
        if remaining <= 0 or replay.state == State.EXPIRED:
            return private({'state': State.EXPIRED})
        result = {'state': replay.state, 'gaps': replay.gaps}
        if replay.state in (State.READY, State.PARTIAL) and replay.output_key:
            seconds = min(remaining, storage.PLAYBACK_TTL)
            try:
                result.update(url=storage.signed_url(replay.output_key, seconds), expires_in=seconds)
            except (ValueError, BotoCoreError, ClientError):
                return private(status=503)
        return private(result)
