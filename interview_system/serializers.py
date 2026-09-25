"""
Serializers for the interview_system app.

Read-only response serializers for Clerk-authenticated user payloads,
and Job / JobSkill serializers for the job posting API.
"""

from typing import TYPE_CHECKING, Any
import re
import math

from django.utils import timezone
from django.db import transaction
from rest_framework import serializers

from .models import (
    Application,
    CandidateProfile,
    Interview,
    Job,
    JobSkill,
    Question,
    RecruiterProfile,
    Resume,
    ScoringRubric,
    User,
)
from .resumes.validation import validate_resume_upload

if TYPE_CHECKING:
    from .models import ParsedResume


def validate_phone_format(value: str) -> str:
    """Accept optional '+' followed by 7–15 digits, or an empty phone."""
    phone = value.strip()
    if phone and not re.fullmatch(r"\+?\d{7,15}", phone):
        raise serializers.ValidationError("Use 7–15 digits, optionally prefixed with '+'.")
    return phone


class UserResponseSerializer(serializers.ModelSerializer):
    """Read-only user payload returned for current user endpoints."""

    class Meta:  # type: ignore
        model = User
        fields = [
            "id",
            "clerk_id",
            "email",
            "first_name",
            "last_name",
            "role",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = list(fields)


class UserSummarySerializer(serializers.ModelSerializer):
    """Read-only summary of user identity details included in profile responses."""

    class Meta:  # type: ignore
        model = User
        fields = ["id", "email", "first_name", "last_name", "role", "created_at"]
        read_only_fields = list(fields)


try:
    from drf_spectacular.types import OpenApiTypes
    from drf_spectacular.utils import extend_schema_field
    _extend_image_field = extend_schema_field(OpenApiTypes.STR)
except ImportError:
    def _extend_image_field(cls): return cls


@_extend_image_field
class ImageOrURLField(serializers.Field):
    """
    Field that accepts either an uploaded image file or a URL string,
    and serializes to the logo URL or null.
    """

    def to_representation(self, value: Any) -> str | None:
        if value.company_logo_url:
            return value.company_logo_url
        logo = value.company_logo
        if not logo:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(logo.url) if request else logo.url

    def to_internal_value(self, data: Any) -> Any:
        if data is None or data == "":
            return {"company_logo": None, "company_logo_url": ""}
        if hasattr(data, "read") or hasattr(data, "chunks"):
            file_field = serializers.ImageField()
            return {"company_logo": file_field.to_internal_value(data), "company_logo_url": ""}
        if isinstance(data, str):
            url_field = serializers.URLField()
            return {"company_logo": None, "company_logo_url": url_field.to_internal_value(data)}
        raise serializers.ValidationError("Expected an image file or a valid URL string.")


class RecruiterProfileSerializer(serializers.ModelSerializer):
    """
    Serializer for RecruiterProfile GET and PATCH endpoints.
    Includes read-only nested user info.
    """

    user = UserSummarySerializer(read_only=True)
    company_logo = ImageOrURLField(source="*", required=False, allow_null=True)
    company_size = serializers.ChoiceField(
        choices=[
            ("1-10", "1-10"),
            ("11-50", "11-50"),
            ("51-200", "51-200"),
            ("201-500", "201-500"),
            ("500+", "500+"),
        ],
        required=False,
        allow_blank=True,
    )

    class Meta:  # type: ignore
        model = RecruiterProfile
        fields = [
            "user",
            "phone",
            "company_name",
            "company_logo",
            "industry",
            "position",
            "company_size",
        ]
        read_only_fields = ["user"]

    def validate_company_name(self, value: str) -> str:
        if value is not None and not value.strip():
            raise serializers.ValidationError("Company name cannot be blank.")
        return value.strip() if value else ""

    def validate_phone(self, value: str) -> str:
        return validate_phone_format(value)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not self.partial:
            company_name = attrs.get(
                "company_name",
                getattr(self.instance, "company_name", "") if self.instance else "",
            )
            if not company_name or not str(company_name).strip():
                raise serializers.ValidationError(
                    {"company_name": "Company name is required."}
                )
        return attrs


class CandidateProfileSerializer(serializers.ModelSerializer):
    """
    Serializer for CandidateProfile GET and PATCH endpoints.
    Includes read-only nested user info.
    """

    user = UserSummarySerializer(read_only=True)
    headline = serializers.CharField(
        max_length=150,
        required=False,
        allow_blank=True,
    )
    years_experience = serializers.IntegerField(
        min_value=0,
        required=False,
        allow_null=True,
    )
    linkedin_url = serializers.URLField(
        required=False,
        allow_blank=True,
    )
    portfolio_url = serializers.URLField(
        required=False,
        allow_blank=True,
    )

    class Meta:  # type: ignore
        model = CandidateProfile
        fields = [
            "user",
            "phone",
            "location",
            "linkedin_url",
            "portfolio_url",
            "years_experience",
            "headline",
        ]
        read_only_fields = ["user"]

    def validate_phone(self, value: str) -> str:
        return validate_phone_format(value)



# ── Job Skill ────────────────────────────────────────────────

class JobSkillSerializer(serializers.ModelSerializer):
    """Serializer for structured skill entries attached to a Job."""

    class Meta:  # type: ignore
        model = JobSkill
        fields = ["id", "job", "skill_name", "is_required"]
        read_only_fields = ["id", "job"]

    def validate_skill_name(self, value: str) -> str:
        job_pk = None
        instance = self.instance
        if isinstance(instance, JobSkill):
            job_pk = instance.job_id
        elif "job" in self.context:
            job = self.context["job"]
            job_pk = getattr(job, "pk", job)
        elif "job_id" in self.context:
            job_pk = self.context["job_id"]
        elif "view" in self.context and hasattr(self.context["view"], "kwargs"):
            view_kwargs = self.context["view"].kwargs
            job_pk = view_kwargs.get("job_pk") or view_kwargs.get("pk")

        if job_pk is not None:
            duplicates = JobSkill.objects.filter(job_id=job_pk, skill_name__iexact=value.strip())
            if isinstance(instance, JobSkill):
                duplicates = duplicates.exclude(pk=instance.pk)
            if duplicates.exists():
                raise serializers.ValidationError("This skill already exists for this job.")
        return value


class ScoringRubricSerializer(serializers.ModelSerializer):
    """Admin-facing rubric representation; activation has its own action."""

    weight_match = serializers.FloatField(min_value=0.0, max_value=1.0)
    weight_interview = serializers.FloatField(min_value=0.0, max_value=1.0)
    weight_behavioral = serializers.FloatField(min_value=0.0, max_value=1.0)

    class Meta:  # type: ignore
        model = ScoringRubric
        fields = ["id", "name", "weight_match", "weight_interview", "weight_behavioral", "active", "created_at"]
        read_only_fields = ["id", "active", "created_at"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # 1e-6 accepts ordinary float representation error while rejecting
        # materially unbalanced scoring configurations.
        weights = (attrs["weight_match"], attrs["weight_interview"], attrs["weight_behavioral"])
        if not math.isclose(math.fsum(weights), 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise serializers.ValidationError({"weights": "Weights must sum to 1.0 within 1e-6."})
        return attrs


# ── Job ──────────────────────────────────────────────────────

class JobSerializer(serializers.ModelSerializer):
    """
    Read serializer for Job with nested read-only job_skills.

    Used for list/retrieve responses. Includes the recruiter's
    company name for display convenience.
    """

    job_skills = JobSkillSerializer(many=True, read_only=True)
    recruiter_company = serializers.CharField(
        source="recruiter.company_name",
        read_only=True,
    )

    class Meta:  # type: ignore
        model = Job
        fields = [
            "id",
            "recruiter",
            "recruiter_company",
            "title",
            "description",
            "requirements",
            "skills_required",
            "location",
            "job_type",
            "experience_level",
            "status",
            "deadline",
            "created_at",
            "updated_at",
            "job_skills",
        ]
        read_only_fields = [
            "id",
            "recruiter",
            "created_at",
            "updated_at",
        ]

    def validate_skills_required(
        self, value: list[str],
    ) -> list[str]:
        """Ensure every entry in skills_required is a non-empty string."""
        cleaned: list[str] = []
        for idx, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise serializers.ValidationError(
                    f"Item at index {idx} must be a non-empty string."
                )
            cleaned.append(item.strip())
        return cleaned


class JobCreateUpdateSerializer(serializers.ModelSerializer):
    """
    Write serializer for creating / updating a Job.

    ``recruiter`` is set automatically from the request user's
    RecruiterProfile in the viewset — it is excluded from input.
    ``status`` is managed exclusively via /publish/ and /close/ endpoints.
    """

    title = serializers.CharField(max_length=200, required=True)
    description = serializers.CharField(required=True)
    requirements = serializers.CharField(required=False, allow_blank=True, default="")
    location = serializers.CharField(required=True)
    job_type = serializers.ChoiceField(choices=Job.JobType.choices, required=True)
    experience_level = serializers.ChoiceField(
        choices=Job.ExperienceLevel.choices, required=True
    )
    skills_required = serializers.ListField(
        child=serializers.CharField(max_length=100),
        min_length=1,
        required=True,
    )
    deadline = serializers.DateField(required=True)

    class Meta:  # type: ignore
        model = Job
        fields = [
            "title",
            "description",
            "requirements",
            "skills_required",
            "location",
            "job_type",
            "experience_level",
            "deadline",
        ]

    def validate_skills_required(self, value: list[str]) -> list[str]:
        if not value or len(value) < 1:
            raise serializers.ValidationError("At least one skill is required.")
        cleaned: list[str] = []
        for idx, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise serializers.ValidationError(
                    f"Item at index {idx} must be a non-empty string."
                )
            cleaned.append(item.strip())
        return cleaned

    def validate_deadline(self, value: Any) -> Any:
        if value and value <= timezone.now().date():
            raise serializers.ValidationError("Deadline must be a future date.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if self.instance and self.initial_data and "status" in self.initial_data:
            raise serializers.ValidationError(
                {"status": "Status cannot be updated directly. Use /publish/ or /close/."}
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data: dict[str, Any]) -> Job:
        validated_data["status"] = Job.Status.DRAFT
        skills = validated_data.get("skills_required", [])
        job = super().create(validated_data)
        self._sync_job_skills(job, skills)
        return job

    @transaction.atomic
    def update(self, instance: Job, validated_data: dict[str, Any]) -> Job:
        skills = validated_data.get("skills_required", None)
        job = super().update(instance, validated_data)
        if skills is not None:
            self._sync_job_skills(job, skills)
        return job

    def _sync_job_skills(self, job: Job, skills: list[str]) -> None:
        unique_skills = list(dict.fromkeys(skills))
        existing = {row.skill_name: row for row in JobSkill.objects.select_for_update().filter(job=job)}
        for name in unique_skills:
            row = existing.get(name)
            if row is None:
                JobSkill.objects.create(job=job, skill_name=name, is_required=True)
            elif not row.is_required:
                row.is_required = True
                row.save(update_fields=["is_required"])
        JobSkill.objects.filter(job=job, is_required=True).exclude(skill_name__in=unique_skills).delete()
        if job.skills_required != unique_skills:
            job.skills_required = unique_skills
            job.save(update_fields=["skills_required"])

    def to_representation(self, instance: Job) -> dict[str, Any]:
        return JobSerializer(instance, context=self.context).data


# ═══════════════════════════════════════════════════════════════
#  Application Serializers
# ═══════════════════════════════════════════════════════════════

class ApplicationCreateSerializer(serializers.Serializer):
    """
    Input serializer for POST /api/applications/.

    Handles multipart upload (resume_file), consent validation, and
    atomically creates both Resume and Application rows.

    NOTE: Application.resume is a deliberate schema extension beyond the
    original Phase 2 SDD. FR-11 describes attaching a resume at apply-time
    but never names the binding field. We formalize it as an explicit FK
    here. This is intentional, not an oversight.
    """

    job = serializers.UUIDField()
    resume_file = serializers.FileField()
    consent_given = serializers.BooleanField()

    def validate_job(self, value: Any) -> Job:
        """Resolve UUID to Job instance and verify it's accepting applications."""
        try:
            job = Job.objects.get(pk=value)
        except Job.DoesNotExist:
            raise serializers.ValidationError("Job not found.")
        if job.status != Job.Status.ACTIVE:
            raise serializers.ValidationError(
                "This job is not accepting applications."
            )
        return job

    def validate_consent_given(self, value: bool) -> bool:
        if value is not True:
            raise serializers.ValidationError(
                "Consent is required. Your resume text will be sent to a "
                "third-party AI service (Google Gemini) for parsing and "
                "skill extraction before matching. You must agree to proceed "
                "(per NFR-11)."
            )
        return value

    def validate_resume_file(self, value: Any) -> Any:
        return validate_resume_upload(value)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Check (candidate, job) uniqueness at the application layer."""
        request = self.context["request"]
        candidate_profile = getattr(request.user, "candidate_profile", None)
        if candidate_profile is None:
            raise serializers.ValidationError(
                "Candidate profile not found for this user."
            )

        job = attrs["job"]
        if Application.objects.filter(candidate=candidate_profile, job=job).exists():
            raise serializers.ValidationError(
                {"job": "You have already applied to this job."}
            )

        attrs["_candidate_profile"] = candidate_profile
        return attrs

    def create(self, validated_data: dict[str, Any]) -> Application:
        from django.db import transaction

        candidate_profile = validated_data["_candidate_profile"]
        job = validated_data["job"]
        resume_file = validated_data["resume_file"]

        with transaction.atomic():
            resume = Resume.objects.create(
                candidate=candidate_profile,
                file=resume_file,
            )
            application = Application.objects.create(
                candidate=candidate_profile,
                job=job,
                resume=resume,
                status=Application.Status.APPLIED,
            )
        return application

    def to_representation(self, instance: Application) -> dict[str, Any]:
        """
        After creation, serialize the response using the detail serializer
        rather than the input fields (resume_file, consent_given, etc.).
        """
        return ApplicationDetailSerializer(instance, context=self.context).data


class ApplicationListSerializer(serializers.ModelSerializer):
    """
    Lightweight read-only serializer for listing applications.
    Shows job title for display convenience without nesting the full Job object.
    """

    job_title = serializers.CharField(source="job.title", read_only=True)

    class Meta:  # type: ignore
        model = Application
        fields = ["id", "job", "job_title", "status", "created_at"]
        read_only_fields = list(fields)


class ApplicationDetailSerializer(serializers.ModelSerializer):
    """
    Full read-only serializer for retrieving a single application.
    Includes candidate info, job title, resume ID, and timestamps.
    """

    job_title = serializers.CharField(source="job.title", read_only=True)
    candidate_email = serializers.EmailField(
        source="candidate.user.email", read_only=True
    )
    resume_id = serializers.UUIDField(source="resume.pk", read_only=True)

    class Meta:  # type: ignore
        model = Application
        fields = [
            "id",
            "candidate",
            "candidate_email",
            "job",
            "job_title",
            "resume_id",
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = list(fields)


class ApplicationAdvanceSerializer(serializers.Serializer):
    """
    Input serializer for PATCH /api/applications/{id}/advance/.

    Validates the transitions owned by the generic advance endpoint.

    Scheduling is the sole owner of SCREENED → INTERVIEWED, so that
    transition is deliberately rejected here.
    """

    status = serializers.ChoiceField(choices=Application.Status.choices)

    def validate_status(self, value: str) -> str:
        application: Application = self.context["application"]
        current = application.status

        if current == Application.Status.SCREENED:
            raise serializers.ValidationError(
                "SCREENED applications become INTERVIEWED only when an "
                "interview is scheduled through POST /api/interviews/."
            )

        allowed = {
            Application.Status.APPLIED: Application.Status.SCREENED,
            Application.Status.INTERVIEWED: Application.Status.DECISION,
        }
        if current == Application.Status.DECISION:
            raise serializers.ValidationError(
                "Application is already in terminal status 'DECISION'. "
                "No further advancement is possible."
            )
        if current not in allowed:
            raise serializers.ValidationError(
                f"Application is in an unrecognized status: {current}."
            )

        next_status = str(allowed[current])

        if value != next_status:
            raise serializers.ValidationError(
                f"Invalid status transition. Current status is '{current}'. "
                f"The next valid status is '{next_status}', "
                f"but '{value}' was provided."
            )

        return value


class StartVoiceSessionResponseSerializer(serializers.Serializer):
    """A local session always starts, even when Retell is unavailable."""

    interview_session_id = serializers.UUIDField(
        read_only=True,
        help_text="Local session ID used for the Phase 8 /analysis/{interview_session_id}/ WebSocket.",
    )
    retell_session_id = serializers.CharField(
        read_only=True, allow_null=True,
        help_text="Retell call ID; null when voice creation failed and the interview proceeds without Retell.",
    )


class InterviewCreateSerializer(serializers.Serializer):
    """Input contract for recruiter interview scheduling."""

    application = serializers.UUIDField()
    scheduled_at = serializers.DateTimeField()


class InterviewQuestionSerializer(serializers.ModelSerializer):
    """Read-only question representation nested in an interview response."""

    class Meta:  # type: ignore
        model = Question
        fields = ["id", "text", "category", "source", "approved"]
        read_only_fields = list(fields)


class QuestionPatchSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    text = serializers.CharField(required=False, allow_blank=False)
    approved = serializers.BooleanField(required=False)

    def to_internal_value(self, data):
        if isinstance(data, dict) and set(data) - {"id", "text", "approved"}:
            raise serializers.ValidationError("Only id, text, and approved may be supplied.")
        return super().to_internal_value(data)

    def validate(self, attrs):
        if not {"text", "approved"}.intersection(attrs):
            raise serializers.ValidationError("Supply text or approved for each question.")
        return attrs


class InterviewQuestionsPatchSerializer(serializers.Serializer):
    questions = QuestionPatchSerializer(many=True, allow_empty=False)

    def validate_questions(self, items):
        ids = [item["id"] for item in items]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Question IDs must be unique within the request.")
        return items


class InterviewSerializer(serializers.ModelSerializer):
    """Read-only scheduled interview response."""

    questions = InterviewQuestionSerializer(many=True, read_only=True)

    class Meta:  # type: ignore
        model = Interview
        fields = [
            "id",
            "application",
            "status",
            "scheduled_at",
            "questions",
            "created_at",
            "updated_at",
        ]
        read_only_fields = list(fields)


# ═══════════════════════════════════════════════════════════════
#  Resume Detail Serializer (Phase 5)
# ═══════════════════════════════════════════════════════════════

class ResumeDetailSerializer(serializers.Serializer):
    """
    Read-only serializer for GET /api/resumes/{id}/.

    Returns parsed resume data from the linked ParsedResume row.
    All parsed fields are null while status is STORED or PENDING.
    """

    id = serializers.UUIDField(read_only=True)
    status = serializers.CharField(read_only=True)
    processing_error = serializers.CharField(read_only=True)
    skills = serializers.SerializerMethodField()
    education = serializers.SerializerMethodField()
    experience = serializers.SerializerMethodField()
    certifications = serializers.SerializerMethodField()
    match_score = serializers.SerializerMethodField()
    matched_skills = serializers.SerializerMethodField()
    missing_skills = serializers.SerializerMethodField()

    def _get_parsed(self, obj: Resume) -> "ParsedResume | Any | None":
        """Cached accessor for the related ParsedResume."""
        if not hasattr(obj, "_cached_parsed"):
            try:
                setattr(obj, "_cached_parsed", getattr(obj, "parsed_data", None))
            except Exception:
                setattr(obj, "_cached_parsed", None)
        return getattr(obj, "_cached_parsed", None)

    def get_skills(self, obj: Resume) -> list | None:
        parsed = self._get_parsed(obj)
        return parsed.skills if parsed else None

    def get_education(self, obj: Resume) -> list | None:
        parsed = self._get_parsed(obj)
        return parsed.education if parsed else None

    def get_experience(self, obj: Resume) -> list | None:
        parsed = self._get_parsed(obj)
        return parsed.experience if parsed else None

    def get_certifications(self, obj: Resume) -> list | None:
        parsed = self._get_parsed(obj)
        return parsed.certifications if parsed else None

    def get_match_score(self, obj: Resume) -> float | None:
        parsed = self._get_parsed(obj)
        return parsed.match_score if parsed else None

    def get_matched_skills(self, obj: Resume) -> list | None:
        parsed = self._get_parsed(obj)
        return parsed.matched_skills if parsed else None

    def get_missing_skills(self, obj: Resume) -> list | None:
        parsed = self._get_parsed(obj)
        return parsed.missing_skills if parsed else None


class ResumeListSerializer(serializers.ModelSerializer):
    """Serializer for stored candidate resumes; applications queue processing separately."""

    filename = serializers.SerializerMethodField()
    file_url = serializers.SerializerMethodField()

    class Meta:  # type: ignore
        model = Resume
        fields = ["id", "file", "filename", "file_url", "status", "processing_error", "uploaded_at"]
        read_only_fields = ["id", "status", "processing_error", "uploaded_at"]

    def validate_file(self, value: Any) -> Any:
        if self.instance and Application.objects.filter(resume=self.instance).exists():
            raise serializers.ValidationError(
                "A resume submitted with an application cannot be replaced."
            )
        return validate_resume_upload(value)

    def get_filename(self, obj: Resume) -> str:
        if obj.file:
            import os
            return os.path.basename(obj.file.name)
        return "resume.pdf"

    def get_file_url(self, obj: Resume) -> str | None:
        if not obj.file:
            return None
        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(obj.file.url)
        return obj.file.url
