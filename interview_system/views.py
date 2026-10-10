from __future__ import annotations

import logging

from typing import Any, Sequence, cast
from uuid import UUID

import django_filters
from django.db import IntegrityError, models, transaction
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import filters, generics, mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet, ModelViewSet

from .integrations import retell_client
from .models import InterviewSession
from .models import Application, CandidateProfile, Interview, Job, JobSkill, RecruiterProfile, Resume, ScoringRubric, User
from .permissions import IsAdmin, IsApplicationAccessible, IsCandidate, IsJobOwner, IsRecruiter, IsResumeAccessible
from .serializers import (
    ApplicationAdvanceSerializer,
    ScreeningActionSerializer,
    ScreeningRetrySerializer,
    ApplicationCreateSerializer,
    ApplicationDetailSerializer,
    ApplicationListSerializer,
    CandidateProfileSerializer,
    StartVoiceSessionResponseSerializer,
    InterviewCreateSerializer,
    InterviewSerializer,
    InterviewQuestionSerializer,
    InterviewQuestionsPatchSerializer,
    JobCreateUpdateSerializer,
    JobSerializer,
    JobSkillSerializer,
    RecruiterProfileSerializer,
    ResumeDetailSerializer,
    ResumeListSerializer,
    ScoringRubricSerializer,
    UserResponseSerializer,
    UserUpdateSerializer,
)



logger = logging.getLogger(__name__)


class StandardResultsPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


@extend_schema_view(
    list=extend_schema(tags=["Interviews"], summary="List interviews"),
    retrieve=extend_schema(tags=["Interviews"], summary="Retrieve an interview"),
)
class InterviewViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    GenericViewSet
):
    """Interview listing, scheduling, and owned-question review."""

    lookup_value_regex = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    queryset = Interview.objects.none()
    pagination_class = StandardResultsPagination

    def get_permissions(self) -> list[Any]:
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsAuthenticated(), IsRecruiter()]

    def get_serializer_class(self):
        if self.action == "create":
            return InterviewCreateSerializer
        return InterviewSerializer

    def get_queryset(self):
        user = self.request.user
        if not user or not user.is_authenticated:
            return Interview.objects.none()

        if getattr(user, "role", None) == User.Role.CANDIDATE:
            return Interview.objects.filter(
                application__candidate__user=user
            ).order_by("-created_at")

        if getattr(user, "role", None) == User.Role.RECRUITER:
            return Interview.objects.filter(
                application__job__recruiter__user=user
            ).order_by("-created_at")

        return Interview.objects.none()

    @extend_schema(
        tags=["Interviews"],
        summary="Start a live interview session",
        request=None,
        responses={
            201: StartVoiceSessionResponseSerializer,
            400: OpenApiResponse(description="Interview is not SCHEDULED, has no approved questions, or already has a session."),
            403: OpenApiResponse(description="Recruiter does not own the linked job."),
            404: OpenApiResponse(description="Interview does not exist."),
        },
    )
    @action(detail=True, methods=["post"], url_path="start-voice-session")
    def start_voice_session(self, request, pk=None):
        with transaction.atomic():
            # Use the same parent lock as question review/generation. A second
            # start waits, then sees LIVE instead of creating another session.
            # Deliberately do not use the owner-filtered get_queryset(): this
            # action requires 403 for an existing interview owned by someone else.
            interview = get_object_or_404(
                Interview.objects.select_for_update(), pk=pk,
            )
            if interview.application.job.recruiter.user_id != request.user.pk:
                raise PermissionDenied("You do not own this interview's linked job.")
            if interview.status != Interview.Status.SCHEDULED:
                raise ValidationError({
                    "status": (
                        "Interview must be SCHEDULED to start a voice session; "
                        f"current status is '{interview.status}'."
                    ),
                })
            approved_questions = list(
                interview.questions.select_for_update().filter(approved=True)
                .order_by("created_at", "pk")
            )
            # FR-22: unapproved questions never satisfy the gate or reach Retell.
            if not approved_questions:
                raise ValidationError({
                    "questions": "At least one approved question is required to start the interview.",
                })
            if InterviewSession.objects.filter(interview=interview).exists():
                raise ValidationError({"session": "This interview already has a session."})
            session = InterviewSession.objects.create(
                interview=interview, started_at=timezone.now(),
            )
            interview.retell_session_id = None
            try:
                interview.retell_session_id = retell_client.create_session(
                    interview, approved_questions,
                )
            except retell_client.RetellUnavailableError as exc:
                # Expected external failure is caught INSIDE atomic: the local
                # session and LIVE status must still commit for recruiter-led
                # questions and Phase 8 analysis. DB/programming errors propagate.
                # Step 1 guarantees sanitized exception messages; never log a
                # request, response, credentials, or provider exception traceback.
                logger.warning(
                    "Retell session creation failed; proceeding without voice: "
                    "interview_id=%s interview_session_id=%s reason=%s",
                    interview.pk, session.pk, str(exc),
                )
            interview.status = Interview.Status.LIVE
            interview.save(update_fields=["status", "retell_session_id", "updated_at"])
            result = StartVoiceSessionResponseSerializer({
                "interview_session_id": session.pk,
                "retell_session_id": interview.retell_session_id,
            }).data
        return Response(result, status=status.HTTP_201_CREATED)

    @extend_schema(
        methods=["GET"], responses={200: InterviewQuestionSerializer(many=True)},
    )
    @extend_schema(
        methods=["PATCH"], request=InterviewQuestionsPatchSerializer,
        responses={200: InterviewQuestionSerializer(many=True)},
    )
    @action(detail=True, methods=["get", "patch"], url_path="questions")
    def questions(self, request, pk=None):
        if request.method == "GET":
            interview = self.get_object()
            return Response(InterviewQuestionSerializer(
                interview.questions.order_by("created_at", "pk"), many=True,
            ).data)

        with transaction.atomic():
            # Share this parent lock with generation, serializing changes to
            # the question set without locking throughout a Gemini request.
            interview = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
            serializer = InterviewQuestionsPatchSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            items = serializer.validated_data["questions"]
            rows = {q.pk: q for q in interview.questions.select_for_update().filter(
                pk__in=[item["id"] for item in items]
            )}
            if len(rows) != len(items):
                raise ValidationError({"questions": "Every question ID must belong to this interview."})
            for item in items:
                question = rows[item["id"]]
                for field in ("text", "approved"):
                    if field in item:
                        setattr(question, field, item[field])
            interview.questions.model.objects.bulk_update(list(rows.values()), ["text", "approved"])
            result = InterviewQuestionSerializer(
                interview.questions.order_by("created_at", "pk"), many=True,
            ).data
        return Response(result)

    @extend_schema(
        tags=["Interviews"],
        summary="Schedule an interview",
        request=InterviewCreateSerializer,
        responses={201: InterviewSerializer},
    )
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        application_id = serializer.validated_data["application"]
        scheduled_at = serializer.validated_data["scheduled_at"]

        recruiter_profile = getattr(request.user, "recruiter_profile", None)
        if recruiter_profile is None:
            recruiter_profile = RecruiterProfile.objects.filter(user=request.user).first()

        with transaction.atomic():
            try:
                application = (
                    Application.objects.select_for_update()
                    .select_related("job")
                    .get(pk=application_id)
                )
            except Application.DoesNotExist:
                raise ValidationError(
                    {"application": "Application does not exist."}
                )

            if (
                recruiter_profile is None
                or application.job.recruiter_id != recruiter_profile.pk
            ):
                raise PermissionDenied(
                    "You do not have permission to schedule an interview for this application."
                )

            if application.status not in (Application.Status.SCREENED, Application.Status.SHORTLISTED):
                raise ValidationError(
                    {
                        "application": (
                            "Application must be in SCREENED status (legacy) or SHORTLISTED status before scheduling "
                            f"an interview; current status is '{application.status}'."
                        )
                    }
                )

            if scheduled_at <= timezone.now():
                raise ValidationError(
                    {"scheduled_at": "Interview must be scheduled for a future time."}
                )

            interview = Interview.objects.create(
                application=application,
                status=Interview.Status.SCHEDULED,
                scheduled_at=scheduled_at,
            )
            application.status = Application.Status.INTERVIEWED
            application.save(update_fields=["status", "updated_at"])

            from .tasks.interviewing import generate_questions

            _generate = cast(Any, generate_questions)
            transaction.on_commit(
                lambda interview_id=str(interview.pk): _generate.delay(interview_id)
            )
            # Snapshot before dispatch, including in eager development mode.
            result = InterviewSerializer(interview, context={"request": request}).data

        return Response(
            result,
            status=status.HTTP_201_CREATED,
        )


class ScoringRubricViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, GenericViewSet):
    """Admin-only rubric listing, creation, and serialized activation."""

    queryset = ScoringRubric.objects.all().order_by("created_at", "pk")
    serializer_class = ScoringRubricSerializer
    permission_classes = [IsAuthenticated, IsAdmin]

    @action(detail=True, methods=["post"])
    def activate(self, request: Request, pk: str | None = None) -> Response:
        with transaction.atomic():
            # Lock every existing rubric in a stable order. Concurrent
            # activations therefore serialize; the partial unique constraint
            # is a database backstop for writes outside this endpoint.
            locked = list(ScoringRubric.objects.order_by("pk").select_for_update())
            target = next((rubric for rubric in locked if str(rubric.pk) == pk), None)
            if target is None:
                raise NotFound("Scoring rubric not found.")
            ScoringRubric.objects.filter(active=True).exclude(pk=target.pk).update(active=False)
            ScoringRubric.objects.filter(pk=target.pk).update(active=True)
            target.active = True
        return Response(self.get_serializer(target).data, status=status.HTTP_200_OK)


class JobFilter(django_filters.FilterSet):
    title = django_filters.CharFilter(field_name="title", lookup_expr="icontains")
    title__icontains = django_filters.CharFilter(field_name="title", lookup_expr="icontains")
    location = django_filters.CharFilter(field_name="location", lookup_expr="icontains")
    location__icontains = django_filters.CharFilter(field_name="location", lookup_expr="icontains")
    job_type = django_filters.ChoiceFilter(choices=Job.JobType.choices)
    experience_level = django_filters.ChoiceFilter(choices=Job.ExperienceLevel.choices)
    status = django_filters.ChoiceFilter(choices=Job.Status.choices)
    created_at__gte = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_at__lte = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = Job
        fields = [
            "title",
            "title__icontains",
            "job_type",
            "experience_level",
            "location",
            "location__icontains",
            "status",
            "created_at__gte",
            "created_at__lte",
        ]


class LoginRoleView(APIView):
    """Authenticate a prospective app session before frontend activation."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        return Response(UserResponseSerializer(request.user).data)


class MeView(APIView):
    """
    Return current authenticated user's details.
    Requires a valid Clerk JWT Bearer token in the Authorization header.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Auth — User"],
        summary="Get current user",
        description=(
            "Returns the profile of the currently authenticated Clerk user.\n\n"
            "Requires `Authorization: Bearer <clerk_jwt>` header."
        ),
        responses={
            200: OpenApiResponse(
                response=UserResponseSerializer,
                description="Authenticated user profile.",
            ),
            401: OpenApiResponse(description="Missing, invalid, or expired Clerk JWT."),
        },
    )
    def get(self, request: Request) -> Response:
        serializer = UserResponseSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        tags=["Auth — User"],
        summary="Update current user",
        description="Update profile details for the currently authenticated user.",
        request=UserUpdateSerializer,
        responses={
            200: UserResponseSerializer,
            400: OpenApiResponse(description="Invalid data."),
        },
    )
    def patch(self, request: Request) -> Response:
        serializer = UserUpdateSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserResponseSerializer(request.user).data, status=status.HTTP_200_OK)


# ═══════════════════════════════════════════════════════════════
#  Recruiter Profile View
# ═══════════════════════════════════════════════════════════════

@extend_schema_view(
    get=extend_schema(
        tags=["Recruiter Profile"],
        summary="Retrieve recruiter profile",
        description="Returns self-owned recruiter profile for the authenticated recruiter.",
        responses={
            200: RecruiterProfileSerializer,
            401: OpenApiResponse(description="Missing, invalid, or expired Clerk JWT."),
            403: OpenApiResponse(description="User is not a recruiter."),
            404: OpenApiResponse(description="Recruiter profile not found."),
        },
    ),
    put=extend_schema(
        tags=["Recruiter Profile"],
        summary="Update recruiter profile (full)",
        description="Full update of the self-owned recruiter profile.",
        responses={
            200: RecruiterProfileSerializer,
            400: OpenApiResponse(description="Invalid field input."),
            401: OpenApiResponse(description="Missing, invalid, or expired Clerk JWT."),
            403: OpenApiResponse(description="User is not a recruiter."),
        },
    ),
    patch=extend_schema(
        tags=["Recruiter Profile"],
        summary="Update recruiter profile (partial)",
        description="Partial update of the self-owned recruiter profile.",
        responses={
            200: RecruiterProfileSerializer,
            400: OpenApiResponse(description="Invalid field input."),
            401: OpenApiResponse(description="Missing, invalid, or expired Clerk JWT."),
            403: OpenApiResponse(description="User is not a recruiter."),
        },
    ),
)
class RecruiterProfileView(generics.RetrieveUpdateAPIView):
    """
    GET / PATCH /api/recruiters/profile/
    Retrieve or update the authenticated recruiter's profile.
    Self-owned only (no {id} in URL).
    """

    permission_classes = [IsAuthenticated, IsRecruiter]
    serializer_class = RecruiterProfileSerializer

    def get_object(self) -> RecruiterProfile:
        profile = getattr(self.request.user, "recruiter_profile", None)
        if profile is None:
            profile = RecruiterProfile.objects.filter(user=self.request.user).first()
        if profile is None:
            raise NotFound("Recruiter profile not found for this user.")
        return profile


# ═══════════════════════════════════════════════════════════════
#  Candidate Profile View
# ═══════════════════════════════════════════════════════════════

@extend_schema_view(
    get=extend_schema(
        tags=["Candidate Profile"],
        summary="Retrieve candidate profile",
        description="Returns self-owned candidate profile for the authenticated candidate.",
        responses={
            200: CandidateProfileSerializer,
            401: OpenApiResponse(description="Missing, invalid, or expired Clerk JWT."),
            403: OpenApiResponse(description="User is not a candidate."),
            404: OpenApiResponse(description="Candidate profile not found."),
        },
    ),
    put=extend_schema(
        tags=["Candidate Profile"],
        summary="Update candidate profile (full)",
        description="Full update of the self-owned candidate profile.",
        responses={
            200: CandidateProfileSerializer,
            400: OpenApiResponse(description="Invalid field input."),
            401: OpenApiResponse(description="Missing, invalid, or expired Clerk JWT."),
            403: OpenApiResponse(description="User is not a candidate."),
        },
    ),
    patch=extend_schema(
        tags=["Candidate Profile"],
        summary="Update candidate profile (partial)",
        description="Partial update of the self-owned candidate profile.",
        responses={
            200: CandidateProfileSerializer,
            400: OpenApiResponse(description="Invalid field input."),
            401: OpenApiResponse(description="Missing, invalid, or expired Clerk JWT."),
            403: OpenApiResponse(description="User is not a candidate."),
        },
    ),
)
class CandidateProfileView(generics.RetrieveUpdateAPIView):
    """
    GET / PATCH /api/candidates/profile/
    Retrieve or update the authenticated candidate's profile.
    Self-owned only (no {id} in URL).
    """

    permission_classes = [IsAuthenticated, IsCandidate]
    serializer_class = CandidateProfileSerializer

    def get_object(self) -> CandidateProfile:
        profile = getattr(self.request.user, "candidate_profile", None)
        if profile is None:
            profile = CandidateProfile.objects.filter(user=self.request.user).first()
        if profile is None:
            raise NotFound("Candidate profile not found for this user.")
        return profile


# ═══════════════════════════════════════════════════════════════
#  Job ViewSet
# ═══════════════════════════════════════════════════════════════

@extend_schema_view(
    list=extend_schema(tags=["Jobs"], summary="List jobs"),
    retrieve=extend_schema(tags=["Jobs"], summary="Retrieve a job"),
    create=extend_schema(tags=["Jobs"], summary="Create a job"),
    update=extend_schema(tags=["Jobs"], summary="Update a job"),
    partial_update=extend_schema(tags=["Jobs"], summary="Partially update a job"),
    destroy=extend_schema(tags=["Jobs"], summary="Delete a job"),
)
class JobViewSet(ModelViewSet):
    """
    JobViewSet offering 5 actions + status transition actions:
    - POST /api/jobs/ (IsRecruiter) -> create DRAFT job
    - GET /api/jobs/ (Auth) -> candidates see ACTIVE only, recruiters see their own jobs
    - GET /api/jobs/{id}/ (Auth) -> candidates see ACTIVE only (404 otherwise), recruiters see own jobs or ACTIVE
    - PATCH /api/jobs/{id}/ (IsRecruiter + IsOwner) -> edit fields, status read-only (400 if passed)
    - DELETE /api/jobs/{id}/ (IsRecruiter + IsOwner) -> hard delete DRAFT jobs only (400 if ACTIVE/CLOSED)
    - POST /api/jobs/{id}/publish/ (IsRecruiter + IsOwner) -> DRAFT -> ACTIVE
    - POST /api/jobs/{id}/close/ (IsRecruiter + IsOwner) -> ACTIVE -> CLOSED
    """

    pagination_class = StandardResultsPagination
    filter_backends: Sequence[Any] = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = JobFilter
    ordering_fields = ["created_at", "deadline", "title"]

    def get_queryset(self):
        user = self.request.user
        if not user or not user.is_authenticated:
            return Job.objects.filter(status=Job.Status.ACTIVE)

        if getattr(user, "role", None) == User.Role.RECRUITER:
            recruiter_profile = getattr(user, "recruiter_profile", None)
            if self.action == "retrieve":
                return Job.objects.filter(
                    models.Q(recruiter=recruiter_profile) | models.Q(status=Job.Status.ACTIVE)
                )
            return Job.objects.filter(recruiter=recruiter_profile)

        return Job.objects.filter(status=Job.Status.ACTIVE)

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return JobCreateUpdateSerializer
        return JobSerializer

    def get_permissions(self) -> list[Any]:
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        if self.action == "create":
            return [IsAuthenticated(), IsRecruiter()]
        return [IsAuthenticated(), IsRecruiter(), IsJobOwner()]

    def perform_create(self, serializer) -> None:
        recruiter_profile = getattr(self.request.user, "recruiter_profile", None)
        if recruiter_profile is None:
            recruiter_profile = RecruiterProfile.objects.filter(user=self.request.user).first()
        serializer.save(recruiter=recruiter_profile)

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        job = self.get_object()
        if job.status != Job.Status.DRAFT:
            return Response(
                {"detail": "Only DRAFT jobs can be deleted. Use /close/ to retire active jobs."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        tags=["Jobs"],
        summary="Publish a job",
        description="Transitions a DRAFT job to ACTIVE status.",
        responses={
            200: JobSerializer,
            400: OpenApiResponse(description="Preconditions for publishing unmet."),
            403: OpenApiResponse(description="Forbidden."),
            404: OpenApiResponse(description="Job not found."),
        },
    )
    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated, IsRecruiter, IsJobOwner])
    def publish(self, request: Request, pk: str | None = None) -> Response:
        job = self.get_object()
        if job.status != Job.Status.DRAFT:
            return Response(
                {"detail": "Only DRAFT jobs can be published."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not job.skills_required or len(job.skills_required) < 1:
            return Response(
                {"detail": "Job must have at least one required skill to be published."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not job.deadline or job.deadline <= timezone.now().date():
            return Response(
                {"detail": "Deadline must be in the future to publish job."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        job.status = Job.Status.ACTIVE
        job.save()
        serializer = JobSerializer(job, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        tags=["Jobs"],
        summary="Close a job",
        description="Transitions an ACTIVE job to CLOSED status.",
        responses={
            200: JobSerializer,
            400: OpenApiResponse(description="Job is not in ACTIVE status."),
            403: OpenApiResponse(description="Forbidden."),
            404: OpenApiResponse(description="Job not found."),
        },
    )
    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated, IsRecruiter, IsJobOwner])
    def close(self, request: Request, pk: str | None = None) -> Response:
        job = self.get_object()
        if job.status != Job.Status.ACTIVE:
            return Response(
                {"detail": "Only ACTIVE jobs can be closed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        job.status = Job.Status.CLOSED
        job.save()
        serializer = JobSerializer(job, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)


# ═══════════════════════════════════════════════════════════════
#  JobSkill ViewSet (nested under Job)
# ═══════════════════════════════════════════════════════════════

@extend_schema_view(
    list=extend_schema(tags=["Jobs"], summary="List skills for a job"),
    retrieve=extend_schema(tags=["Jobs"], summary="Retrieve a job skill"),
    create=extend_schema(tags=["Jobs"], summary="Add a skill to a job"),
    update=extend_schema(tags=["Jobs"], summary="Update a job skill"),
    partial_update=extend_schema(tags=["Jobs"], summary="Partially update a job skill"),
    destroy=extend_schema(tags=["Jobs"], summary="Remove a skill from a job"),
)
class JobSkillViewSet(ModelViewSet):
    """
    CRUD viewset for JobSkill entries, scoped to a parent Job.

    URL pattern: ``/api/jobs/<job_pk>/skills/``

    - **List / Retrieve**: Public for ACTIVE jobs; authenticated owner
      for any status.
    - **Create / Update / Delete**: Only the owning recruiter.
    """

    serializer_class = JobSkillSerializer

    def get_queryset(self):
        if self.action in ("list", "retrieve"):
            user = self.request.user
            visible_jobs = Job.objects.filter(status=Job.Status.ACTIVE)
            if user and user.is_authenticated and getattr(user, "role", None) == User.Role.RECRUITER:
                visible_jobs = Job.objects.filter(
                    models.Q(status=Job.Status.ACTIVE) | models.Q(recruiter__user=user)
                )
        else:
            visible_jobs = Job.objects.filter(recruiter__user=self.request.user)
        job = get_object_or_404(visible_jobs, pk=self.kwargs["job_pk"])
        return JobSkill.objects.filter(job=job)

    def get_permissions(self) -> list[Any]:
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAuthenticated(), IsRecruiter(), IsJobOwner()]

    def perform_create(self, serializer) -> None:
        """Attach the skill to the parent job from the URL."""
        job = get_object_or_404(
            Job.objects.filter(recruiter__user=self.request.user),
            pk=self.kwargs["job_pk"],
        )
        with transaction.atomic():
            job = Job.objects.select_for_update().get(pk=job.pk)
            job.screening_policy_version += 1
            job.save(update_fields=["screening_policy_version"])
            try:
                with transaction.atomic():
                    skill = serializer.save(job=job)
            except IntegrityError as exc:
                raise ValidationError({"skill_name": "This skill already exists for this job."}) from exc
            required = list(job.skills_required)
            if skill.is_required and skill.skill_name not in required:
                required.append(skill.skill_name)
            elif not skill.is_required and skill.skill_name in required:
                required.remove(skill.skill_name)
            if required != job.skills_required:
                job.skills_required = required
                job.save(update_fields=["skills_required"])

    def perform_update(self, serializer) -> None:
        with transaction.atomic():
            job = Job.objects.select_for_update().get(pk=serializer.instance.job_id)
            job.screening_policy_version += 1
            job.save(update_fields=["screening_policy_version"])
            old_name = serializer.instance.skill_name
            old_required = serializer.instance.is_required
            try:
                with transaction.atomic():
                    skill = serializer.save()
            except IntegrityError as exc:
                raise ValidationError({"skill_name": "This skill already exists for this job."}) from exc
            required = list(job.skills_required)
            if old_required:
                required = [name for name in required if name != old_name]
            if skill.is_required and skill.skill_name not in required:
                required.append(skill.skill_name)
            if required != job.skills_required:
                job.skills_required = required
                job.save(update_fields=["skills_required"])

    def perform_destroy(self, instance) -> None:
        with transaction.atomic():
            job = Job.objects.select_for_update().get(pk=instance.job_id)
            job.screening_policy_version += 1
            job.save(update_fields=["screening_policy_version"])
            name, was_required = instance.skill_name, instance.is_required
            instance.delete()
            if was_required and name in job.skills_required:
                job.skills_required = [skill for skill in job.skills_required if skill != name]
                job.save(update_fields=["skills_required"])


# ═══════════════════════════════════════════════════════════════
#  Application ViewSet
# ═══════════════════════════════════════════════════════════════

@extend_schema_view(
    create=extend_schema(
        tags=["Applications"],
        summary="Submit an application",
        description="Candidate submits a job application with resume upload.",
    ),
    list=extend_schema(
        tags=["Applications"],
        summary="List applications",
        description=(
            "Candidates see their own applications. "
            "Recruiters must pass ?job={id} and own the job."
        ),
    ),
    retrieve=extend_schema(
        tags=["Applications"],
        summary="Retrieve an application",
        description="Visible to the owning candidate or the job's recruiter.",
    ),
)
class ApplicationViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    GenericViewSet,
):
    """
    Application endpoints — no update/destroy via standard CRUD.

    Uses GenericViewSet + explicit mixins instead of ModelViewSet because
    applications are immutable once created; lifecycle changes occur through
    the explicit ``/advance/`` action and interview scheduling.

    Endpoints:
        POST   /api/applications/               — IsCandidate
        GET    /api/applications/               — IsAuthenticated (role-scoped)
        GET    /api/applications/{id}/           — IsAuthenticated + IsApplicationAccessible
        PATCH  /api/applications/{id}/advance/   — IsRecruiter + job-owner
    """

    pagination_class = StandardResultsPagination
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    # ── Serializer dispatch ────────────────────────────────────

    def get_serializer_class(self):
        if self.action == "create":
            return ApplicationCreateSerializer
        if self.action == "retrieve":
            return ApplicationDetailSerializer
        if self.action == "advance":
            return ApplicationAdvanceSerializer
        return ApplicationListSerializer

    # ── Permissions dispatch ───────────────────────────────────

    def get_permissions(self) -> list[Any]:
        if self.action == "create":
            return [IsAuthenticated(), IsCandidate()]
        if self.action in ("advance", "shortlist", "reject", "rescreen"):
            return [IsAuthenticated(), IsRecruiter()]
        if self.action == "retrieve":
            return [IsAuthenticated(), IsApplicationAccessible()]
        # list
        return [IsAuthenticated()]

    # ── Queryset — role-scoped ─────────────────────────────────

    def get_queryset(self):
        from decimal import Decimal, InvalidOperation
        from uuid import UUID
        user = cast(User, self.request.user)
        qs = Application.objects.select_related("job", "resume", "candidate__user", "current_assessment").prefetch_related("interviews").order_by("-created_at", "-pk")
        if user.role == User.Role.CANDIDATE:
            qs = qs.filter(candidate__user=user)
        elif user.role == User.Role.RECRUITER:
            qs = qs.filter(job__recruiter__user=user)
        else:
            return qs.none()
        if self.action not in ("list", "summary"):
            return qs
        params = self.request.query_params
        supported = {"job", "status", "processing_status", "min_match_score", "eligibility", "eligible_for_interview", "ordering", "page", "page_size", "format"}
        if set(params) - supported:
            raise ValidationError({"filters": "Unsupported filter parameter."})
        job_id = params.get("job")
        if user.role == User.Role.RECRUITER and not job_id:
            raise ValidationError({"job": "Recruiters must provide a ?job={id} query parameter."})
        if job_id:
            try:
                UUID(job_id)
            except (ValueError, TypeError):
                raise ValidationError({"job": "Invalid job ID."})
            if user.role == User.Role.RECRUITER and not Job.objects.filter(pk=job_id, recruiter__user=user).exists():
                raise PermissionDenied("This job is not accessible.")
            qs = qs.filter(job_id=job_id)
        for param, field, valid in (
            ("status", "status", set(Application.Status.values)),
            ("processing_status", "current_assessment__status", {"QUEUED", "PARSING", "MATCHING", "SCREENING", "SUCCEEDED", "FAILED"}),
            ("eligibility", "current_assessment__eligibility", {"PASS", "FAIL", "UNKNOWN"}),
        ):
            if param in params:
                if params[param] not in valid:
                    raise ValidationError({param: "Invalid filter value."})
                qs = qs.filter(**{field: params[param]})
        if "min_match_score" in params:
            try:
                score = Decimal(params["min_match_score"])
                if not score.is_finite() or not 0 <= score <= 100:
                    raise ValueError()
            except (InvalidOperation, ValueError):
                raise ValidationError({"min_match_score": "Use a number from 0 to 100."})
            qs = qs.filter(current_assessment__status="SUCCEEDED", current_assessment__match_score__gte=score)
        if "eligible_for_interview" in params:
            if params["eligible_for_interview"] not in ("true", "false"):
                raise ValidationError({"eligible_for_interview": "Use true or false."})
            ready = models.Q(status__in=["SHORTLISTED", "SCREENED"], interviews__isnull=True)
            qs = qs.filter(ready) if params["eligible_for_interview"] == "true" else qs.exclude(ready)
        ordering = params.get("ordering", "-created_at")
        mapping = {"created_at": "created_at", "-created_at": "-created_at", "match_score": "current_assessment__match_score", "-match_score": "-current_assessment__match_score"}
        if ordering not in mapping:
            raise ValidationError({"ordering": "Invalid ordering."})
        return qs.order_by(mapping[ordering], "pk")

    @action(detail=False, methods=["get"])
    def summary(self, request):
        qs = self.get_queryset()
        return Response({
            "total": qs.count(),
            "by_status": dict(qs.order_by().values("status").annotate(n=models.Count("pk")).values_list("status", "n")),
            "by_processing_status": dict(qs.order_by().values("current_assessment__status").annotate(n=models.Count("pk")).values_list("current_assessment__status", "n")),
        })

    def _screening_action(self, request, target):
        from .screening import transition
        obj = self.get_object()
        payload = ScreeningActionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        app = transition(obj.pk, target, actor=request.user, **payload.validated_data)
        return Response(ApplicationDetailSerializer(app, context={"request": request}).data)

    @extend_schema(request=ScreeningActionSerializer, responses={200: ApplicationDetailSerializer})
    @action(detail=True, methods=["post"])
    def shortlist(self, request, pk=None):
        return self._screening_action(request, "SHORTLISTED")

    @extend_schema(request=ScreeningActionSerializer, responses={200: ApplicationDetailSerializer})
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._screening_action(request, "REJECTED")

    @extend_schema(request=ScreeningRetrySerializer, responses={202: ApplicationDetailSerializer})
    @action(detail=True, methods=["post"], url_path="retry-screening")
    def retry_screening(self, request, pk=None):
        return self._restart_screening(request, rescreen=False)

    @extend_schema(request=ScreeningRetrySerializer, responses={202: ApplicationDetailSerializer})
    @action(detail=True, methods=["post"])
    def rescreen(self, request, pk=None):
        return self._restart_screening(request, rescreen=True)

    def _restart_screening(self, request, *, rescreen):
        from datetime import timedelta
        from rest_framework.exceptions import Throttled
        from .screening import ACTIVE_STAGES, ScreeningConflict, create_assessment
        obj = self.get_object()
        payload = ScreeningRetrySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        with transaction.atomic():
            app = Application.objects.select_for_update().get(pk=obj.pk)
            current = app.current_assessment
            if app.status not in ("APPLIED", "UNDER_REVIEW"):
                raise ScreeningConflict("This application cannot be reprocessed.")
            if current and current.status in ACTIVE_STAGES:
                # A replay of the immediately preceding request returns that same work.
                if data["expected_version"] not in (app.version, app.version - 1):
                    raise ScreeningConflict()
                return Response(ApplicationDetailSerializer(app, context={"request": request}).data, status=202)
            if data["expected_version"] != app.version:
                raise ScreeningConflict()
            if not current or (rescreen and (app.status != "UNDER_REVIEW" or current.status != "SUCCEEDED")) or (not rescreen and current.status != "FAILED"):
                raise ScreeningConflict("This action does not match the current assessment state.")
            if current.recovery_action == "CONTACT_SUPPORT":
                from django.conf import settings
                if not settings.GEMINI_API_KEY:
                    raise ValidationError("Resume processing is not configured. Contact support before retrying.")
            if app.assessments.filter(created_at__gte=timezone.now() - timedelta(minutes=10)).count() >= 4:
                raise Throttled(wait=600, detail="Too many screening attempts. Try again later.")
            replacement = data.get("resume_id")
            if replacement:
                if rescreen or request.user.role != User.Role.CANDIDATE or not data.get("consent_given"):
                    raise ValidationError("Only the candidate can replace a failed resume with processing consent.")
                resume = get_object_or_404(Resume, pk=replacement, candidate_id=app.candidate_id)
                app.resume = resume
                app.save(update_fields=["resume"])
            if current.recovery_action == "REPLACE_RESUME" and not replacement:
                raise ValidationError({"resume_id": "Upload a readable replacement resume first."})
            create_assessment(app.pk, use_current_policy=rescreen)
            app.refresh_from_db()
        return Response(ApplicationDetailSerializer(app, context={"request": request}).data, status=202)

    # ── Advance action — forward-only lifecycle ────────────────

    @extend_schema(
        tags=["Applications"],
        summary="Advance application status",
        description=(
            "Screening transitions require expected_version and a reason for overrides/rejection. "
            "Prefer the dedicated shortlist/reject endpoints. INTERVIEWED → DECISION remains supported. "
            "Scheduling occurs only through POST /api/interviews/."
        ),
        request=ApplicationAdvanceSerializer,
        responses={
            200: ApplicationDetailSerializer,
            400: OpenApiResponse(description="Invalid status transition."),
            403: OpenApiResponse(description="Forbidden."),
            404: OpenApiResponse(description="Application not found."),
        },
    )
    @action(
        detail=True,
        methods=["patch"],
        url_path="advance",
        permission_classes=[IsAuthenticated, IsRecruiter],
    )
    def advance(self, request: Request, pk: str | None = None) -> Response:
        application = self.get_object()
        target = request.data.get("status")
        if target in ("SHORTLISTED", "SCREENED", "REJECTED"):
            return self._screening_action(request, "SHORTLISTED" if target == "SCREENED" else target)
        # Preserve the legacy downstream decision contract for the later interview phase.
        serializer = ApplicationAdvanceSerializer(data=request.data, context={"application": application})
        serializer.is_valid(raise_exception=True)
        application.status = serializer.validated_data["status"]
        application.save(update_fields=["status", "updated_at"])
        return Response(ApplicationDetailSerializer(application, context={"request": request}).data)



# ═══════════════════════════════════════════════════════════════
#  Resume Detail View (Phase 5)
# ═══════════════════════════════════════════════════════════════

class ResumeDetailView(generics.RetrieveAPIView):
    """
    GET /api/resumes/{id}/

    Returns the parsed resume data and match score for a given resume.
    Accessible by the owning candidate or a recruiter whose job is linked
    via an Application to this resume.

    Response includes:
      - status: STORED / PENDING / PARSED / FAILED
      - skills, education, experience, certifications (null until parsed)
      - match_score, matched_skills, missing_skills (null until computed)
    """

    serializer_class = ResumeDetailSerializer
    permission_classes = [IsAuthenticated, IsResumeAccessible]
    queryset = Resume.objects.all()
    lookup_field = "pk"

    @extend_schema(
        tags=["Resumes"],
        summary="Retrieve resume parsing results",
        description=(
            "Returns the parsed resume data (skills, education, experience, "
            "certifications) and match score. All parsed fields are null "
            "while a standalone upload is STORED or processing is PENDING."
        ),
        responses={
            200: ResumeDetailSerializer,
            401: OpenApiResponse(description="Missing or invalid JWT."),
            403: OpenApiResponse(description="Not authorized to view this resume."),
            404: OpenApiResponse(description="Resume not found."),
        },
    )
    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().get(request, *args, **kwargs)


class CandidateResumeViewSet(ModelViewSet):
    """
    CRUD ViewSet for candidate-managed, unsubmitted resumes.
    Strictly scoped to request.user.candidate_profile.
    """

    queryset = Resume.objects.all()
    serializer_class = ResumeListSerializer
    permission_classes = [IsAuthenticated, IsCandidate]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        user = self.request.user
        candidate_profile = getattr(user, "candidate_profile", None)
        if candidate_profile is None:
            candidate_profile = CandidateProfile.objects.filter(user=user).first()
        if candidate_profile is None:
            return Resume.objects.none()
        return Resume.objects.filter(candidate=candidate_profile).order_by("-uploaded_at")

    @staticmethod
    def _dispatch_parse(resume_id: str) -> None:
        """Dispatch parsing and record a failure when dispatch cannot complete."""
        import logging
        from django.conf import settings
        from .tasks.parsing import parse_resume
        _parse = cast(Any, parse_resume)
        try:
            _parse.delay(resume_id=resume_id)
        except Exception as exc:  # noqa: BLE001
            mode = "inline task" if settings.CELERY_TASK_ALWAYS_EAGER else "broker dispatch"
            logging.getLogger(__name__).exception(
                "Resume parsing %s failed for resume_id=%s", mode, resume_id,
            )
            Resume.objects.filter(pk=resume_id, status=Resume.Status.PENDING).update(
                status=Resume.Status.FAILED,
                processing_error=f"{mode} failed: {type(exc).__name__}: {exc}"[:500],
            )

    def perform_create(self, serializer):
        user = self.request.user
        candidate_profile = getattr(user, "candidate_profile", None)
        if candidate_profile is None:
            candidate_profile = CandidateProfile.objects.filter(user=user).first()
        if candidate_profile is None:
            raise PermissionDenied("Candidate profile not found.")

        with transaction.atomic():
            resume = serializer.save(candidate=candidate_profile, status=Resume.Status.PENDING)
            resume_id = str(resume.id)
            transaction.on_commit(lambda: self._dispatch_parse(resume_id))

    def perform_update(self, serializer):
        with transaction.atomic():
            resume = serializer.save(status=Resume.Status.PENDING)
            resume_id = str(resume.id)
            transaction.on_commit(lambda: self._dispatch_parse(resume_id))

    def destroy(self, request, *args, **kwargs):
        resume = self.get_object()
        if Application.objects.filter(resume=resume).exists():
            return Response(
                {"detail": "A submitted resume cannot be deleted."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "A submitted resume cannot be deleted."},
                status=status.HTTP_409_CONFLICT,
            )
