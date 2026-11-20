"""Live, role-protected data for the SkillSense admin console."""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from redis import Redis
from redis.exceptions import RedisError
from rest_framework import generics, serializers
from rest_framework.exceptions import ValidationError
from rest_framework.filters import SearchFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .audit import record_audit
from .models import (
    ApplicationAssessment, BehavioralAnalysis, FrameAnalysis, FrameJob, Interview,
    Job, Resume, TranscriptRevision, User,
)
from .permissions import IsAdmin
from .views import StandardResultsPagination


class AdminConsoleMixin:
    permission_classes = [IsAuthenticated, IsAdmin]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response


class AdminOverviewView(AdminConsoleMixin, APIView):
    def get(self, request):
        now = timezone.now()
        since = now - timedelta(days=7)
        users = User.objects.aggregate(total=Count("pk"), new=Count("pk", filter=Q(created_at__gte=since)))
        completed = Interview.objects.filter(status=Interview.Status.DONE)
        flagged = BehavioralAnalysis.objects.filter(report_schema_version=0).exclude(
            integrity_flags=[]
        ).filter(Exists(FrameAnalysis.objects.filter(session_id=OuterRef("session_id"))))
        assessments = ApplicationAssessment.objects.all()
        recent_signals = [
            {"id": session, "title": "Interview integrity signal", "created_at": created}
            for session, created in flagged.filter(created_at__gte=since)
            .order_by("-created_at", "-pk").values_list("session_id", "created_at")[:6]
        ]
        alerts = list(recent_signals)
        for model, title, timestamp in (
            (assessments.filter(status="FAILED"), "Application screening failed", "updated_at"),
            (completed.filter(evaluation_status="FAILED"), "Interview evaluation failed", "updated_at"),
        ):
            alerts.extend(
                {"id": pk, "title": title, "created_at": created}
                for pk, created in model.filter(**{f"{timestamp}__gte": since})
                .order_by(f"-{timestamp}", "-pk").values_list("pk", timestamp)[:6]
            )
        alerts.sort(key=lambda alert: (alert["created_at"], alert["id"]), reverse=True)
        try:
            with Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1) as broker:
                broker_available = bool(broker.ping())
        except (RedisError, ValueError):
            broker_available = False
        return Response({
            "updated_at": timezone.now(),
            "total_users": users["total"],
            "new_users_7d": users["new"],
            "active_jobs": Job.objects.filter(status=Job.Status.ACTIVE).count(),
            "completed_interviews": completed.count(),
            "flagged_sessions": flagged.count(),
            "broker_available": broker_available,
            "pending_frame_jobs": FrameJob.objects.filter(
                state=FrameJob.State.PENDING, expires_at__gt=now,
            ).count(),
            "screening_pending": assessments.filter(status__in=("QUEUED", "PARSING", "MATCHING", "SCREENING")).count(),
            "screening_succeeded_7d": assessments.filter(status="SUCCEEDED", completed_at__gte=since).count(),
            "recent_alerts": alerts[:6],
            "recent_integrity_signals": recent_signals,
        })


class AdminUserSerializer(serializers.ModelSerializer):
    company = serializers.CharField(source="recruiter_profile.company_name", default="", read_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name", "role", "is_active", "created_at", "company")
        read_only_fields = fields


class AdminAccountStatusSerializer(serializers.Serializer):
    is_active = serializers.BooleanField()

    def validate(self, attrs):
        if set(self.initial_data) != {"is_active"} or type(self.initial_data["is_active"]) is not bool:
            raise serializers.ValidationError("Send only is_active as a JSON boolean.")
        return attrs


class AdminUserListView(AdminConsoleMixin, generics.ListAPIView):
    serializer_class = AdminUserSerializer
    pagination_class = StandardResultsPagination
    filter_backends = [SearchFilter]
    search_fields = ["email", "first_name", "last_name", "recruiter_profile__company_name"]
    queryset = User.objects.select_related("recruiter_profile").order_by("-created_at", "-pk")

    def get_queryset(self):
        queryset = super().get_queryset()
        role = self.request.query_params.get("role")
        active = self.request.query_params.get("is_active")
        if role:
            if role not in User.Role.values:
                raise ValidationError({"role": "Unknown role."})
            queryset = queryset.filter(role=role)
        if active is not None:
            if active not in ("true", "false"):
                raise ValidationError({"is_active": "Use true or false."})
            queryset = queryset.filter(is_active=active == "true")
        return queryset


class AdminUserStatusView(AdminConsoleMixin, APIView):
    def patch(self, request, pk):
        serializer = AdminAccountStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        active = serializer.validated_data["is_active"]
        with transaction.atomic():
            user = get_object_or_404(User.objects.select_for_update(), pk=pk)
            if user.role == User.Role.ADMIN:
                raise ValidationError({"is_active": "Admin accounts are managed separately."})
            if user.is_active != active:
                user.is_active = active
                user.save(update_fields=["is_active", "updated_at"])
                record_audit(
                    actor=request.user, target=user, changed_fields=["is_active"],
                    action="account.activated" if active else "account.deactivated",
                )
        return Response(AdminUserSerializer(user).data)


class AdminOperationsView(AdminConsoleMixin, APIView):
    def get(self, request):
        now = timezone.now()
        resume_days = getattr(settings, "RETENTION_RESUME_DAYS", 90)
        transcript_days = getattr(settings, "RETENTION_TRANSCRIPT_DAYS", 90)
        return Response({
            "models": {
                "Resume parsing": settings.GEMINI_MODEL_NAME,
                "Question generation": settings.GEMINI_QUESTION_MODEL_NAME,
                "Answer evaluation": settings.GEMINI_EVALUATION_MODEL_NAME,
                "Semantic matching": settings.SBERT_MODEL_NAME,
            },
            "behavioral_observation_enabled": settings.INTERVIEW_ANALYSIS_VISION_ENABLED,
            "retention": [
                {"name": "Resumes", "days": resume_days,
                 "review_due": Resume.objects.filter(uploaded_at__lt=now - timedelta(days=resume_days)).count()},
                {"name": "Transcript revisions", "days": transcript_days,
                 "review_due": TranscriptRevision.objects.filter(created_at__lt=now - timedelta(days=transcript_days)).count()},
            ],
        })
