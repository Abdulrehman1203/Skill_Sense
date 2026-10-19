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
    ApplicationAssessment,
    AuditLogEntry,
    IntegrityThresholds,
    Notification,
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
            "profile_image_url",
            "role",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = list(fields)


class UserUpdateSerializer(serializers.ModelSerializer):
    """Writable serializer for updating current user details."""

    class Meta:  # type: ignore
        model = User
        fields = ["first_name", "last_name", "email", "profile_image_url"]


class UserSummarySerializer(serializers.ModelSerializer):
    """Read-only summary of user identity details included in profile responses."""

    profile_image_url = serializers.SerializerMethodField()

    def get_profile_image_url(self, obj) -> str | None:
        return obj.profile_image_url or None

    class Meta:  # type: ignore
        model = User
        fields = ["id", "email", "first_name", "last_name", "profile_image_url", "role", "created_at"]
        read_only_fields = list(fields)


try:
    from drf_spectacular.types import OpenApiTypes
    from drf_spectacular.utils import extend_schema_field
    _extend_image_field = extend_schema_field(OpenApiTypes.STR)
except ImportError:
    def _extend_image_field(cls): return cls
    def extend_schema_field(field): return lambda f: f


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
    profile_image = serializers.ImageField(
        required=False, write_only=True,
        help_text="Personal profile photo uploaded to Clerk. Returned as user.profile_image_url.",
    )
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
            "profile_image",
            "industry",
            "position",
            "company_size",
        ]
        read_only_fields = ["user"]

    def validate_company_name(self, value: str) -> str:
        if value is not None and not value.strip():
            raise serializers.ValidationError("Company name cannot be blank.")
        return value.strip() if value else ""

    def validate_profile_image(self, value):
        if value.size > 10 * 1024 * 1024:
            raise serializers.ValidationError("Profile picture must be 10 MB or smaller.")
        return value

    def update(self, instance, validated_data):
        from .profile_images import upload_profile_image

        image = validated_data.pop("profile_image", None)
        image_url = upload_profile_image(instance.user, image) if image is not None else None
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            if image_url is not None:
                instance.user.profile_image_url = image_url
                instance.user.save(update_fields=["profile_image_url", "updated_at"])
        return instance

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
    completion_percentage = serializers.SerializerMethodField()

    def get_completion_percentage(self, profile) -> int:
        # Derive completion from the saved profile and its own user's fields.
        text_fields = (
            profile.user.first_name, profile.user.last_name, profile.user.email,
            profile.phone, profile.location, profile.headline,
            profile.linkedin_url, profile.portfolio_url,
        )
        completed = sum(bool((value or "").strip()) for value in text_fields)
        completed += profile.years_experience is not None and profile.years_experience >= 0
        return round(completed / 9 * 100)

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
            "completion_percentage",
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

    match_score = serializers.SerializerMethodField()
    average_match_score = serializers.SerializerMethodField()
    has_applied = serializers.BooleanField(read_only=True, default=False)

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
            "salary",
            "job_type",
            "experience_level",
            "status",
            "deadline",
            "created_at",
            "updated_at",
            "job_skills",
            "match_score",
            "average_match_score",
            "has_applied",
            "screening_threshold", "auto_shortlist_enabled", "non_pass_policy",
            "screening_policy_version", "screening_criteria",
        ]
        read_only_fields = [
            "id",
            "recruiter",
            "created_at",
            "updated_at",
        ]

    def get_match_score(self, obj: Job) -> float | None:
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        candidate = getattr(request.user, "candidate_profile", None)
        if not candidate:
            return None
        app = obj.applications.select_related("current_assessment").filter(candidate=candidate).first()
        if app and app.current_assessment and app.current_assessment.status == "SUCCEEDED" and app.current_assessment.match_score is not None:
            return float(app.current_assessment.match_score)
        return None

    def get_average_match_score(self, obj: Job) -> float | None:
        from django.db.models import Avg
        avg = obj.applications.filter(
            current_assessment__status="SUCCEEDED"
        ).aggregate(avg=Avg("current_assessment__match_score"))["avg"]
        return round(avg, 2) if avg is not None else None

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
    salary = serializers.CharField(required=False, allow_blank=True, default="")
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

    def validate_screening_criteria(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Screening criteria must be a dictionary.")
        if set(value) - {
            "minimum_experience_years", "education_levels", "certifications",
            "manual_review_required", "question_mix", "skill_weights",
        }:
            raise serializers.ValidationError("Unsupported screening criteria.")
        years = value.get("minimum_experience_years")
        if years is not None and (isinstance(years, bool) or not isinstance(years, (float, int)) or not math.isfinite(years) or not 0 <= years <= 80):
            raise serializers.ValidationError("Minimum experience must be a number from 0 to 80.")
        for key in ("education_levels", "certifications"):
            items = value.get(key, [])
            if not isinstance(items, list) or len(items) > 30 or any(not isinstance(s, str) or not s.strip() or len(s) > 150 for s in items):
                raise serializers.ValidationError(f"{key} must contain up to 30 nonempty strings.")
        if "manual_review_required" in value and not isinstance(value["manual_review_required"], bool):
            raise serializers.ValidationError("manual_review_required must be boolean.")
        if "question_mix" in value:
            mix = value["question_mix"]
            if (
                not isinstance(mix, dict)
                or set(mix) - {"technical", "behavioral", "situational"}
                or any(type(count) is not int or not 0 <= count <= 12 for count in mix.values())
                or sum(mix.values()) > 12
            ):
                raise serializers.ValidationError(
                    "Question mix must contain whole-number counts totaling at most 12 "
                    "for technical, behavioral, and situational questions."
                )
        if "skill_weights" in value:
            weights = value["skill_weights"]
            if (
                not isinstance(weights, dict)
                or len(weights) > 20
                or any(
                    not isinstance(name, str) or not name.strip() or len(name) > 100
                    or type(weight) is not int or not 1 <= weight <= 3
                    for name, weight in weights.items()
                )
            ):
                raise serializers.ValidationError(
                    "Skill weights must contain up to 20 named skills with whole-number weights from 1 to 3."
                )
        return value

    class Meta:  # type: ignore
        model = Job
        fields = [
            "title",
            "description",
            "requirements",
            "skills_required",
            "location",
            "salary",
            "job_type",
            "experience_level",
            "deadline",
            "screening_threshold", "auto_shortlist_enabled", "non_pass_policy", "screening_criteria",
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
        from .screening import POLICY_FIELDS
        instance = Job.objects.select_for_update().get(pk=instance.pk)
        if any(name in validated_data and validated_data[name] != getattr(instance, name) for name in POLICY_FIELDS):
            instance.screening_policy_version += 1
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
    resume_file = serializers.FileField(required=False)
    resume_id = serializers.UUIDField(required=False)
    consent_given = serializers.BooleanField()
    cover_note = serializers.CharField(required=False, allow_blank=True, max_length=5000)

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
        if job.deadline and job.deadline < timezone.localdate():
            raise serializers.ValidationError("The application deadline has passed.")
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

        job = attrs.get("job")
        if job and Application.objects.filter(candidate=candidate_profile, job=job).exists():
            raise serializers.ValidationError(
                {"job": "You have already applied to this job."}
            )

        resume_file = attrs.get("resume_file")
        resume_id = attrs.get("resume_id")

        if not resume_file and not resume_id:
            raise serializers.ValidationError(
                "Either a resume file or an existing resume ID must be provided."
            )
            
        if resume_file and resume_id:
            raise serializers.ValidationError(
                "Provide either a resume file or a resume ID, not both."
            )
            
        if resume_id:
            try:
                resume = Resume.objects.get(pk=resume_id, candidate=candidate_profile)
                attrs["resume"] = resume
            except Resume.DoesNotExist:
                raise serializers.ValidationError(
                    {"resume_id": "Resume not found or does not belong to you."}
                )

        attrs["_candidate_profile"] = candidate_profile
        return attrs

    def create(self, validated_data: dict[str, Any]) -> Application:
        from django.db import transaction

        candidate_profile = validated_data["_candidate_profile"]
        job = validated_data["job"]
        resume_file = validated_data.get("resume_file")
        resume = validated_data.get("resume")

        with transaction.atomic():
            job = Job.objects.select_for_update().get(pk=job.pk)
            if job.status != Job.Status.ACTIVE or (job.deadline and job.deadline < timezone.localdate()):
                raise serializers.ValidationError({"job": "This job is no longer accepting applications."})
            if Application.objects.filter(candidate=candidate_profile, job=job).exists():
                from .screening import ScreeningConflict
                raise ScreeningConflict("You have already applied to this job.")
            if resume_file:
                resume = Resume.objects.create(
                    candidate=candidate_profile,
                    file=resume_file,
                    original_filename=getattr(resume_file, 'name', ''),
                )
            application = Application.objects.create(
                candidate=candidate_profile,
                job=job,
                resume=resume,
                cover_note=validated_data.get("cover_note", ""),
                status=Application.Status.APPLIED,
            )
            application.refresh_from_db()
        return application

    def to_representation(self, instance: Application) -> dict[str, Any]:
        """
        After creation, serialize the response using the detail serializer
        rather than the input fields (resume_file, consent_given, etc.).
        """
        return ApplicationDetailSerializer(instance, context=self.context).data


class AssessmentSerializer(serializers.ModelSerializer):
    threshold = serializers.SerializerMethodField()
    policy_version = serializers.SerializerMethodField()

    class Meta:
        model = ApplicationAssessment
        fields = ["id", "revision", "status", "match_score", "matched_skills", "missing_skills",
                  "criteria_results", "eligibility", "outcome", "reasons", "threshold", "policy_version",
                  "error_code", "error_message", "recovery_action", "attempts", "created_at", "completed_at"]
        read_only_fields = fields

    @extend_schema_field(serializers.FloatField(allow_null=True))
    def get_threshold(self, obj) -> float | None:
        return obj.policy.get("screening_threshold")

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_policy_version(self, obj) -> str | None:
        return obj.policy.get("version")


class ApplicationListSerializer(serializers.ModelSerializer):
    integrity_review = serializers.SerializerMethodField()
    job_title = serializers.CharField(source="job.title", read_only=True)
    job_company = serializers.CharField(source="job.recruiter.company_name", read_only=True)
    candidate_email = serializers.EmailField(source="candidate.user.email", read_only=True)
    candidate_name = serializers.SerializerMethodField()
    candidate_profile_image_url = serializers.CharField(
        source="candidate.user.profile_image_url", read_only=True, allow_null=True,
    )
    resume_id = serializers.UUIDField(source="resume.pk", read_only=True)
    assessment = AssessmentSerializer(source="current_assessment", read_only=True)
    match_score = serializers.SerializerMethodField()
    allowed_actions = serializers.SerializerMethodField()
    eligible_for_interview = serializers.SerializerMethodField()

    class Meta:
        model = Application
        fields = ["id", "candidate", "candidate_email", "candidate_name", "candidate_profile_image_url", "job", "job_title", "job_company", "resume_id",
                  "status", "version", "assessment", "match_score", "allowed_actions", "eligible_for_interview",
                  "created_at", "updated_at", "integrity_review"]
        read_only_fields = fields

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_integrity_review(self, obj):
        request = self.context.get("request")
        if not request or request.user.role != User.Role.RECRUITER:
            return None
        from .integrity import review_context, thresholds
        if "integrity_thresholds" not in self.context:
            self.context["integrity_thresholds"] = (
                IntegrityThresholds(look_away_limit=obj.integrity_look_away_limit,
                                    integrity_flag_limit=obj.integrity_flag_limit)
                if hasattr(obj, "integrity_look_away_limit") else thresholds()
            )
        return review_context(obj, self.context["integrity_thresholds"])

    @extend_schema_field(serializers.CharField())
    def get_candidate_name(self, obj) -> str:
        user = obj.candidate.user
        return f"{user.first_name} {user.last_name}".strip() or user.email

    @extend_schema_field(serializers.FloatField(allow_null=True))
    def get_match_score(self, obj) -> float | None:
        a = obj.current_assessment
        return a.match_score if a and a.status == "SUCCEEDED" else None

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_allowed_actions(self, obj) -> list[str]:
        from .screening import permitted_actions
        request = self.context.get("request")
        return permitted_actions(obj, request.user) if request else []

    @extend_schema_field(serializers.BooleanField())
    def get_eligible_for_interview(self, obj) -> bool:
        return obj.status in ("SHORTLISTED", "SCREENED") and not obj.interviews.all()


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "type", "message", "is_read", "created_at"]
        read_only_fields = fields


class NotificationListQuerySerializer(serializers.Serializer):
    is_read = serializers.ChoiceField(choices=["true", "false"], required=False)


class NotificationUnreadCountSerializer(serializers.Serializer):
    unread_count = serializers.IntegerField(min_value=0, read_only=True)


class NotificationMarkReadSerializer(serializers.Serializer):
    is_read = serializers.BooleanField(required=True)

    def validate_is_read(self, value):
        if self.initial_data.get("is_read") is not True:
            raise serializers.ValidationError("Use the JSON boolean true to mark a notification as read.")
        return value

    def validate(self, attrs):
        if set(self.initial_data) - {"is_read"}:
            raise serializers.ValidationError("Only is_read may be updated.")
        return attrs


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLogEntry
        fields = ["actor", "action", "target_type", "target_id", "metadata", "created_at"]
        read_only_fields = fields


class AdminAnalyticsSerializer(serializers.Serializer):
    applications_by_status = serializers.DictField(child=serializers.IntegerField(min_value=0), read_only=True)
    average_score = serializers.FloatField(allow_null=True, read_only=True)
    interview_completion_rate = serializers.FloatField(min_value=0, max_value=1, read_only=True)


class StrictNonNegativeIntegerField(serializers.IntegerField):
    def to_internal_value(self, data):
        if type(data) is not int:
            self.fail("invalid")
        return super().to_internal_value(data)


class IntegrityThresholdsSerializer(serializers.ModelSerializer):
    look_away_limit = StrictNonNegativeIntegerField(min_value=0, max_value=2147483647, required=False)
    integrity_flag_limit = StrictNonNegativeIntegerField(min_value=0, max_value=2147483647, required=False)

    class Meta:
        model = IntegrityThresholds
        fields = ["look_away_limit", "integrity_flag_limit"]

    def validate(self, attrs):
        if set(self.initial_data) - set(self.fields):
            raise serializers.ValidationError("Unknown threshold field.")
        return attrs


class ScoreBreakdownSerializer(serializers.Serializer):
    match = serializers.FloatField(allow_null=True, default=None, read_only=True)
    interview = serializers.FloatField(allow_null=True, default=None, read_only=True)
    behavioral = serializers.FloatField(allow_null=True, default=None, read_only=True)


class CandidateScoreResponseSerializer(serializers.Serializer):
    final_score = serializers.FloatField(allow_null=True, read_only=True)
    breakdown = ScoreBreakdownSerializer(read_only=True)
    explanation = serializers.CharField(read_only=True)


class CandidateScoreQuerySerializer(serializers.Serializer):
    job = serializers.UUIDField(required=False)


class ApplicationRankingSerializer(ApplicationListSerializer):
    """Reuse the candidate identity fields already visible in application lists."""

    final_score = serializers.FloatField(
        source="score.final_score", allow_null=True, default=None, read_only=True,
    )

    class Meta:
        model = Application
        fields = ["id", "candidate", "candidate_email", "candidate_name", "final_score"]
        read_only_fields = fields


class ApplicationDetailSerializer(ApplicationListSerializer):
    history = serializers.SerializerMethodField()

    class Meta(ApplicationListSerializer.Meta):
        fields = ApplicationListSerializer.Meta.fields + ["cover_note", "history"]
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_history(self, obj) -> list[dict]:
        request = self.context.get("request")
        if not request or request.user.role != User.Role.RECRUITER:
            return []
        return list(obj.screening_events.order_by("-created_at").values(
            "source", "from_status", "to_status", "reason", "created_at", "assessment_id")[:50])


class ScreeningActionSerializer(serializers.Serializer):
    expected_version = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(required=False, default="", allow_blank=True, max_length=2000)


class ScreeningRetrySerializer(serializers.Serializer):
    expected_version = serializers.IntegerField(min_value=1)
    resume_id = serializers.UUIDField(required=False)
    consent_given = serializers.BooleanField(required=False, default=False)


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

        if current in (Application.Status.SCREENED, Application.Status.SHORTLISTED):
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
    """Private, short-lived browser credentials for an owned interview."""

    interview_session_id = serializers.UUIDField(
        read_only=True,
        help_text="Local interview session ID.",
    )
    retell_session_id = serializers.CharField(
        read_only=True, allow_null=True,
        help_text="Retell call ID. Failed voice creation returns 503 and no join credentials.",
    )

    call_id = serializers.CharField(read_only=True)
    access_token = serializers.CharField(read_only=True)
    transport = serializers.CharField(read_only=True)
    ice_servers = serializers.ListField(read_only=True)
    expires_at = serializers.IntegerField(read_only=True)


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
    filename = serializers.SerializerMethodField()
    file_url = serializers.SerializerMethodField()
    file_size = serializers.SerializerMethodField()
    uploaded_at = serializers.DateTimeField(read_only=True)
    status = serializers.CharField(read_only=True)
    processing_error = serializers.CharField(read_only=True)
    skills = serializers.SerializerMethodField()
    education = serializers.SerializerMethodField()
    experience = serializers.SerializerMethodField()
    certifications = serializers.SerializerMethodField()
    match_score = serializers.SerializerMethodField()
    matched_skills = serializers.SerializerMethodField()
    missing_skills = serializers.SerializerMethodField()

    def get_filename(self, obj: Resume) -> str | None:
        if obj.original_filename:
            return obj.original_filename
        if obj.file:
            import os
            return os.path.basename(obj.file.name)
        return None

    def get_file_url(self, obj: Resume) -> str | None:
        if not obj.file:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.file.url) if request else obj.file.url

    def get_file_size(self, obj: Resume) -> int | None:
        if not obj.file:
            return None
        try:
            return obj.file.size
        except (OSError, ValueError):
            return None

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

    def create(self, validated_data: dict[str, Any]) -> Resume:
        file_obj = validated_data.get("file")
        if file_obj:
            validated_data["original_filename"] = getattr(file_obj, 'name', '')
        return super().create(validated_data)

    def validate_file(self, value: Any) -> Any:
        if self.instance and (Application.objects.filter(resume=self.instance).exists() or
                              ApplicationAssessment.objects.filter(resume=self.instance).exists()):
            raise serializers.ValidationError(
                "A resume submitted with an application cannot be replaced."
            )
        return validate_resume_upload(value)

    def get_filename(self, obj: Resume) -> str:
        if obj.original_filename:
            return obj.original_filename
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
