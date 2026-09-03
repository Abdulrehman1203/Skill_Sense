from __future__ import annotations

from typing import Any, Sequence

import django_filters
from django.db import models
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import filters, generics, mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet, ModelViewSet

from .models import Application, CandidateProfile, Job, JobSkill, RecruiterProfile, Resume, User
from .permissions import IsApplicationAccessible, IsCandidate, IsJobOwner, IsRecruiter, IsResumeAccessible
from .serializers import (
    ApplicationAdvanceSerializer,
    ApplicationCreateSerializer,
    ApplicationDetailSerializer,
    ApplicationListSerializer,
    CandidateProfileSerializer,
    JobCreateUpdateSerializer,
    JobSerializer,
    JobSkillSerializer,
    RecruiterProfileSerializer,
    ResumeDetailSerializer,
    ResumeListSerializer,
    UserResponseSerializer,
)



class StandardResultsPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


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
        return JobSkill.objects.filter(job_id=self.kwargs["job_pk"])

    def get_permissions(self) -> list[Any]:
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAuthenticated(), IsRecruiter()]

    def perform_create(self, serializer) -> None:
        """Attach the skill to the parent job from the URL."""
        serializer.save(job_id=self.kwargs["job_pk"])


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
    applications are immutable once created; the only mutation is the
    explicit ``/advance/`` action for lifecycle transitions.

    Endpoints:
        POST   /api/applications/               — IsCandidate
        GET    /api/applications/               — IsAuthenticated (role-scoped)
        GET    /api/applications/{id}/           — IsAuthenticated + IsApplicationAccessible
        PATCH  /api/applications/{id}/advance/   — IsRecruiter + job-owner
    """

    pagination_class = StandardResultsPagination

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
        if self.action == "advance":
            return [IsAuthenticated(), IsRecruiter()]
        if self.action == "retrieve":
            return [IsAuthenticated(), IsApplicationAccessible()]
        # list
        return [IsAuthenticated()]

    # ── Queryset — role-scoped ─────────────────────────────────

    def get_queryset(self):
        user = self.request.user

        if getattr(user, "role", None) == User.Role.CANDIDATE:
            candidate_profile = getattr(user, "candidate_profile", None)
            if candidate_profile is None:
                return Application.objects.none()
            return (
                Application.objects
                .filter(candidate=candidate_profile)
                .select_related("job", "resume")
                .order_by("-created_at")
            )

        if getattr(user, "role", None) == User.Role.RECRUITER:
            recruiter_profile = getattr(user, "recruiter_profile", None)
            if recruiter_profile is None:
                return Application.objects.none()

            # For retrieve and advance, return all applications for owned jobs
            if self.action in ("retrieve", "advance"):
                return (
                    Application.objects
                    .filter(job__recruiter=recruiter_profile)
                    .select_related("job", "resume", "candidate__user")
                    .order_by("-created_at")
                )

            # For list, require ?job= param (validated in list() override)
            job_id = self.request.query_params.get("job")
            if job_id:
                return (
                    Application.objects
                    .filter(job_id=job_id, job__recruiter=recruiter_profile)
                    .select_related("job", "resume", "candidate__user")
                    .order_by("-created_at")
                )
            # If no job param, return empty — list() will raise 400 first
            return Application.objects.none()

        return Application.objects.none()

    # ── List override — recruiter must scope by job ────────────

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        user = request.user

        if getattr(user, "role", None) == User.Role.RECRUITER:
            job_id = request.query_params.get("job")
            if not job_id:
                return Response(
                    {"detail": "Recruiters must provide a ?job={id} query parameter."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Validate the job exists and belongs to this recruiter
            recruiter_profile = getattr(user, "recruiter_profile", None)
            if recruiter_profile is None:
                recruiter_profile = RecruiterProfile.objects.filter(user=user).first()

            try:
                job = Job.objects.get(pk=job_id)
            except (Job.DoesNotExist, ValueError):
                return Response(
                    {"detail": "Job not found."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if (
                recruiter_profile is None
                or job.recruiter_id != recruiter_profile.pk
            ):
                raise PermissionDenied(
                    "You do not have permission to view applications for this job."
                )

        return super().list(request, *args, **kwargs)

    # ── Advance action — forward-only lifecycle ────────────────

    @extend_schema(
        tags=["Applications"],
        summary="Advance application status",
        description=(
            "Advances an application through the lifecycle: "
            "APPLIED → SCREENED → INTERVIEWED → DECISION. "
            "Forward-only, no skipping or reverting."
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

        # Object-level check: recruiter must own the job
        recruiter_profile = getattr(request.user, "recruiter_profile", None)
        if (
            recruiter_profile is None
            or application.job.recruiter_id != recruiter_profile.pk
        ):
            raise PermissionDenied(
                "You do not have permission to advance this application."
            )

        serializer = ApplicationAdvanceSerializer(
            data=request.data,
            context={"request": request, "application": application},
        )
        serializer.is_valid(raise_exception=True)

        new_status = serializer.validated_data["status"]

        # TODO: Phase 9 — advancing to INTERVIEWED will trigger Retell
        # session creation. For now, just update the status.
        application.status = new_status
        application.save(update_fields=["status", "updated_at"])

        return Response(
            ApplicationDetailSerializer(application, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )


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
      - status: PENDING / PARSED / FAILED
      - skills, education, experience, certifications (null while PENDING)
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
            "while parsing is in progress (status=PENDING)."
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
    CRUD ViewSet for candidate-managed resumes.
    Strictly scoped to request.user.candidate_profile.
    """

    serializer_class = ResumeListSerializer
    permission_classes = [IsAuthenticated, IsCandidate]

    def get_queryset(self):
        user = self.request.user
        candidate_profile = getattr(user, "candidate_profile", None)
        if candidate_profile is None:
            candidate_profile = CandidateProfile.objects.filter(user=user).first()
        if candidate_profile is None:
            return Resume.objects.none()
        return Resume.objects.filter(candidate=candidate_profile).order_by("-uploaded_at")

    def perform_create(self, serializer):
        user = self.request.user
        candidate_profile = getattr(user, "candidate_profile", None)
        if candidate_profile is None:
            candidate_profile = CandidateProfile.objects.filter(user=user).first()
        if candidate_profile is None:
            raise PermissionDenied("Candidate profile not found.")
        serializer.save(candidate=candidate_profile)


