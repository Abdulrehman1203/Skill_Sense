"""
Core data models for the SkillSense interview/recruitment platform.

Identity is owned entirely by Clerk. Django's User model here is a
plain record keyed by clerk_id — there is no password, no session
login, and no Django auth backend involved. Authentication happens
in core/authentication.py via ClerkJWTAuthentication, which resolves
a request's verified JWT `sub` claim to a row in this table.

Models:
    User               – Clerk-linked identity record (email + role).
    RecruiterProfile   – Extended profile for RECRUITER users.
    CandidateProfile   – Extended profile for CANDIDATE users.
    Job                – A job posting created by a recruiter.
    JobSkill           – Normalized skill entry for a job.
    Resume             – A resume file uploaded by a candidate.
    Application        – A candidate's application to a job.
    ParsedResume       – Output of the resume parsing/matching pipeline.
    Interview          – An interview scheduled/conducted for an application.
    Question           – A single interview question.
    InterviewSession   – The live/recorded session tied 1:1 to an interview.
    Response           – A candidate's answer to a question.
    BehavioralAnalysis – Aggregate behavioral analysis for a session.
    FrameAnalysis      – Per-frame analysis captured during a session.
    ScoringRubric      – Configurable weighting scheme for scores.
    CandidateScore     – Final computed score for an application.
    Notification       – A notification delivered to a user.
    AuditLogEntry      – Immutable audit trail entry.
"""

from __future__ import annotations

import uuid

from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.core.validators import (
    FileExtensionValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models
from django.utils import timezone


# ═══════════════════════════════════════════════════════════════
#  User
# ═══════════════════════════════════════════════════════════════

class User(models.Model):
    """
    Identity record provisioned by the Clerk webhook (user.created).
    Not a Django auth user — Clerk is the sole authentication source.
    """

    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Admin"
        RECRUITER = "RECRUITER", "Recruiter"
        CANDIDATE = "CANDIDATE", "Candidate"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    clerk_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Clerk's user ID (the JWT 'sub' claim). Sole link to identity.",
    )
    email = models.EmailField(
        unique=True,
        max_length=255,
        verbose_name="email address",
    )
    first_name = models.CharField(max_length=150, blank=True, default="")
    last_name = models.CharField(max_length=150, blank=True, default="")
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.CANDIDATE,
    )
    profile_image_url = models.URLField(blank=True, default="", max_length=1024)
    is_active = models.BooleanField(
        default=True,
        help_text="Soft-disable flag for admin deactivation (FR-06). Not a Django auth flag.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "users"
        constraints = [
            models.UniqueConstraint(
                models.functions.Lower(models.functions.Trim("email")),
                name="users_email_normalized_unique",
            ),
        ]
        verbose_name = "user"
        verbose_name_plural = "users"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.email

    # ── DRF permission-class contract ──────────────────────────
    # DRF's IsAuthenticated (and similar) checks these two
    # properties on request.user. Since this model isn't an
    # AbstractBaseUser subclass, we supply them directly rather
    # than pulling in Django's auth machinery for two booleans.
    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False


class ClerkWebhookState(models.Model):
    """Ordering cursor and deletion tombstone for one Clerk identity."""

    clerk_id = models.CharField(max_length=255, primary_key=True)
    last_event_at = models.DateTimeField()
    is_deleted = models.BooleanField(default=False)

    class Meta:
        db_table = "clerk_webhook_states"


# ═══════════════════════════════════════════════════════════════
#  RecruiterProfile
# ═══════════════════════════════════════════════════════════════

class RecruiterProfile(models.Model):
    """One-to-one profile attached to every RECRUITER user."""

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="recruiter_profile",
        primary_key=True,
    )
    phone = models.CharField(max_length=20, blank=True, default="")
    company_name = models.CharField(max_length=255, blank=True, default="")
    company_logo = models.ImageField(
        upload_to="recruiter/logos/",
        blank=True,
        null=True,
    )
    company_logo_url = models.URLField(blank=True, default="")
    industry = models.CharField(max_length=150, blank=True, default="")
    position = models.CharField(max_length=150, blank=True, default="")
    company_size = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        db_table = "recruiter_profiles"
        verbose_name = "recruiter profile"
        verbose_name_plural = "recruiter profiles"

    def __str__(self) -> str:
        return f"{self.user.email} – {self.company_name or 'No Company'}"


# ═══════════════════════════════════════════════════════════════
#  CandidateProfile
# ═══════════════════════════════════════════════════════════════

class CandidateProfile(models.Model):
    """One-to-one profile attached to every CANDIDATE user."""

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="candidate_profile",
        primary_key=True,
    )
    phone = models.CharField(max_length=20, blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="")
    linkedin_url = models.URLField(blank=True, default="")
    portfolio_url = models.URLField(blank=True, default="")
    years_experience = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text="Total years of professional experience.",
    )
    headline = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="A short professional headline (e.g. 'Senior Python Developer').",
    )

    class Meta:
        db_table = "candidate_profiles"
        verbose_name = "candidate profile"
        verbose_name_plural = "candidate profiles"

    def __str__(self) -> str:
        return f"{self.user.email} – {self.headline or 'No Headline'}"


# ═══════════════════════════════════════════════════════════════
#  Job
# ═══════════════════════════════════════════════════════════════

class Job(models.Model):
    """A job posting created by a recruiter."""

    class JobType(models.TextChoices):
        REMOTE = "REMOTE", "Remote"
        ONSITE = "ONSITE", "Onsite"
        HYBRID = "HYBRID", "Hybrid"

    class ExperienceLevel(models.TextChoices):
        ENTRY = "ENTRY", "Entry"
        MID = "MID", "Mid"
        SENIOR = "SENIOR", "Senior"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        CLOSED = "CLOSED", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recruiter = models.ForeignKey(
        RecruiterProfile,
        on_delete=models.CASCADE,
        related_name="jobs",
    )
    title = models.CharField(max_length=255)
    description = models.TextField()
    requirements = models.TextField()
    skills_required = ArrayField(
        models.CharField(max_length=100), default=list, blank=True
    )
    location = models.CharField(max_length=255)
    job_type = models.CharField(max_length=10, choices=JobType.choices)
    experience_level = models.CharField(
        max_length=10, choices=ExperienceLevel.choices
    )
    salary = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    deadline = models.DateField(null=True, blank=True)
    screening_threshold = models.DecimalField(max_digits=5, decimal_places=2, default=60, validators=[MinValueValidator(0), MaxValueValidator(100)])
    auto_shortlist_enabled = models.BooleanField(default=True)
    non_pass_policy = models.CharField(max_length=10, choices=[("REVIEW", "Hold for review"), ("REJECT", "Reject definite failures")], default="REVIEW")
    screening_policy_version = models.PositiveIntegerField(default=1)
    screening_criteria = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "jobs"
        verbose_name = "job"
        verbose_name_plural = "jobs"
        ordering = ["-created_at"]
        constraints = [models.CheckConstraint(condition=models.Q(screening_threshold__gte=0, screening_threshold__lte=100), name="job_screening_threshold_range")]
        indexes = [
            models.Index(fields=["status"], name="idx_job_status"),
            models.Index(fields=["job_type"], name="idx_job_type"),
            models.Index(fields=["deadline"], name="idx_job_deadline"),
        ]

    def __str__(self):
        return f"{self.title} ({self.recruiter})"

    def clean(self):
        super().clean()
        if (
            self.status == self.Status.ACTIVE
            and self.deadline
            and self.deadline < timezone.now().date()
        ):
            raise ValidationError(
                {"deadline": "Deadline cannot be in the past for an active job."}
            )


# ═══════════════════════════════════════════════════════════════
#  JobSkill
# ═══════════════════════════════════════════════════════════════

class JobSkill(models.Model):
    """Normalized skill entry for a job, for structured skill queries."""

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="job_skills")
    skill_name = models.CharField(max_length=100)
    is_required = models.BooleanField(default=True)

    class Meta:
        db_table = "job_skills"
        verbose_name = "job skill"
        verbose_name_plural = "job skills"
        unique_together = ("job", "skill_name")
        indexes = [
            models.Index(fields=["skill_name"], name="idx_jobskill_name"),
        ]

    def __str__(self):
        return f"{self.skill_name} ({'required' if self.is_required else 'optional'})"


# ═══════════════════════════════════════════════════════════════
#  Resume
# ═══════════════════════════════════════════════════════════════

def resume_upload_path(instance, filename):
    """Generate a unique upload path per candidate: resumes/<candidate_pk>/<uuid>.<ext>"""
    ext = filename.split(".")[-1].lower()
    return f"resumes/{instance.candidate_id}/{uuid.uuid4()}.{ext}"


class Resume(models.Model):
    """A resume file uploaded by a candidate."""

    class Status(models.TextChoices):
        STORED = "STORED", "Stored"
        PENDING = "PENDING", "Pending"
        PARSED = "PARSED", "Parsed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey(
        CandidateProfile,
        on_delete=models.CASCADE,
        related_name="resumes",
    )
    file = models.FileField(
        upload_to=resume_upload_path,
        validators=[FileExtensionValidator(allowed_extensions=["pdf", "docx"])],
    )
    original_filename = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="The original name of the uploaded file."
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        help_text="STORED until submitted; application processing is PENDING → PARSED or FAILED.",
    )
    processing_error = models.TextField(blank=True, default="")
    parse_claim = models.UUIDField(null=True, blank=True)
    parse_lease_until = models.DateTimeField(null=True, blank=True)
    content_hash = models.CharField(max_length=64, blank=True)
    parser_version = models.CharField(max_length=100, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    MAX_FILE_SIZE_MB = 5

    class Meta:
        db_table = "resumes"
        verbose_name = "resume"
        verbose_name_plural = "resumes"
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"Resume({self.candidate}) - {self.uploaded_at:%Y-%m-%d}"

    def clean(self):
        super().clean()
        if self.file and self.file.size > self.MAX_FILE_SIZE_MB * 1024 * 1024:
            raise ValidationError(
                {"file": f"File size must not exceed {self.MAX_FILE_SIZE_MB}MB."}
            )


# ═══════════════════════════════════════════════════════════════
#  Application
# ═══════════════════════════════════════════════════════════════

class Application(models.Model):
    """A candidate's application to a job, bound to the resume submitted."""

    class Status(models.TextChoices):
        APPLIED = "APPLIED", "Applied"
        SCREENED = "SCREENED", "Screened"
        UNDER_REVIEW = "UNDER_REVIEW", "Under review"
        SHORTLISTED = "SHORTLISTED", "Shortlisted"
        SCHEDULED = "SCHEDULED", "Interview scheduled"
        IN_PROGRESS = "IN_PROGRESS", "Interview in progress"
        INTERVIEW_FAILED = "INTERVIEW_FAILED", "Interview failed or insufficient evidence"
        REJECTED = "REJECTED", "Rejected"
        INTERVIEWED = "INTERVIEWED", "Interviewed"
        DECISION = "DECISION", "Decision"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey(
        CandidateProfile,
        on_delete=models.CASCADE,
        related_name="applications",
    )
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="applications")
    resume = models.ForeignKey(
        Resume, on_delete=models.PROTECT, related_name="applications"
    )
    cover_note = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.APPLIED
    )
    version = models.PositiveIntegerField(default=1)
    current_assessment = models.ForeignKey("ApplicationAssessment", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "applications"
        verbose_name = "application"
        verbose_name_plural = "applications"
        ordering = ["-created_at"]
        unique_together = ("candidate", "job")
        indexes = [
            models.Index(fields=["status"], name="idx_application_status"),
            models.Index(fields=["job", "status"], name="idx_application_job_status"),
        ]

    def __str__(self):
        return f"{self.candidate} → {self.job} ({self.status})"

    def clean(self):
        super().clean()
        if self.resume_id and self.resume.candidate_id != self.candidate_id:
            raise ValidationError(
                {"resume": "Resume must belong to the applying candidate."}
            )


# ═══════════════════════════════════════════════════════════════
#  ParsedResume
# ═══════════════════════════════════════════════════════════════

class ApplicationAssessment(models.Model):
    """Immutable inputs and versioned job-relative screening results."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="assessments")
    resume = models.ForeignKey(Resume, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    policy = models.JSONField(default=dict)
    status = models.CharField(max_length=16, default="QUEUED", choices=[(s, s.title()) for s in ("QUEUED", "PARSING", "MATCHING", "SCREENING", "SUCCEEDED", "FAILED")])
    match_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    matched_skills = models.JSONField(default=list)
    missing_skills = models.JSONField(default=list)
    criteria_results = models.JSONField(default=list)
    eligibility = models.CharField(max_length=12, blank=True)
    outcome = models.CharField(max_length=24, blank=True)
    reasons = models.JSONField(default=list)
    error_code = models.CharField(max_length=48, blank=True)
    error_message = models.CharField(max_length=300, blank=True)
    recovery_action = models.CharField(max_length=32, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    claim_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    resume_hash = models.CharField(max_length=64, blank=True)
    parser_version = models.CharField(max_length=64, default="gemini-v1")
    matcher_version = models.CharField(max_length=100, blank=True)
    apply_decision = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["application", "revision"], name="unique_assessment_revision"),
            models.CheckConstraint(condition=models.Q(match_score__isnull=True) | models.Q(match_score__gte=0, match_score__lte=100), name="assessment_score_range"),
        ]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]


class ScreeningEvent(models.Model):
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="screening_events")
    assessment = models.ForeignKey(ApplicationAssessment, null=True, on_delete=models.SET_NULL)
    actor = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    source = models.CharField(max_length=20)
    from_status = models.CharField(max_length=24)
    to_status = models.CharField(max_length=24)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ApplicationDecision(models.Model):
    """Explicit, immutable recruiter decision; historical DECISION rows are not inferred."""
    application = models.OneToOneField(Application, primary_key=True, on_delete=models.CASCADE, related_name="hiring_decision")
    outcome = models.CharField(max_length=12, choices=[("HIRE", "Hire"), ("REJECT", "Reject")])
    actor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="application_decisions")
    reason = models.TextField()
    override = models.BooleanField(default=False)
    override_reason = models.TextField(blank=True)
    application_version = models.PositiveIntegerField()
    interview = models.ForeignKey("Interview", null=True, on_delete=models.PROTECT, related_name="decisions")
    evaluation = models.ForeignKey("InterviewEvaluation", null=True, on_delete=models.PROTECT, related_name="decisions")
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Recorded decisions are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Recorded decisions must be preserved.")


class QuestionPreparationIntent(models.Model):
    """Durable qualification handoff; question generation consumes it in G02.

    One assessment can request preparation only once. The linked event records
    the exact application transition that authorized this work; no Interview or
    placeholder Question rows need to exist yet.
    """

    assessment = models.OneToOneField(
        ApplicationAssessment, primary_key=True, on_delete=models.CASCADE,
        related_name="question_preparation_intent",
    )
    screening_event = models.OneToOneField(
        ScreeningEvent, on_delete=models.CASCADE,
        related_name="question_preparation_intent",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "assessment_id"]


class QuestionSet(models.Model):
    """Application-owned preparation with immutable generation inputs."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="question_sets")
    assessment = models.OneToOneField(ApplicationAssessment, null=True, blank=True, on_delete=models.PROTECT, related_name="question_set")
    assessment_revision = models.PositiveIntegerField(null=True)
    revision = models.PositiveIntegerField(default=1)
    input_snapshot = models.JSONField(default=dict)
    input_hash = models.CharField(max_length=64)
    generation_status = models.CharField(max_length=16, default="QUEUED", choices=[(s, s) for s in ("QUEUED", "GENERATING", "READY", "FALLBACK", "FAILED", "LEGACY")])
    error_code = models.CharField(max_length=64, blank=True)
    fallback_reason = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    generation_number = models.PositiveIntegerField(default=1)
    claim_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    approved_at = models.DateTimeField(null=True)
    approved_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="approved_question_sets")
    frozen_at = models.DateTimeField(null=True)
    frozen_revision = models.PositiveIntegerField(null=True)
    legacy = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.get(pk=self.pk)
            immutable = ("application_id", "assessment_id", "assessment_revision", "input_snapshot", "input_hash", "legacy")
            if any(getattr(previous, field) != getattr(self, field) for field in immutable):
                raise ValidationError("Question preparation inputs are immutable; create a new assessment instead.")
            if previous.frozen_at and any(getattr(previous, field) != getattr(self, field)
                    for field in ("revision", "generation_number", "approved_at", "approved_by_id", "frozen_at", "frozen_revision")):
                raise ValidationError("Frozen question-set approval and revision cannot change.")
        super().save(*args, **kwargs)


class QuestionReviewDelivery(models.Model):
    """One durable recruiter-ready notification per generation, recovered by Beat."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE, related_name="review_deliveries")
    generation_number = models.PositiveIntegerField()
    delivered_at = models.DateTimeField(null=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["question_set", "generation_number"], name="unique_question_review_delivery")]


class PreparedQuestion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE, related_name="items")
    text = models.TextField()
    category = models.CharField(max_length=15, choices=[(s, s) for s in ("BEHAVIORAL", "TECHNICAL", "SITUATIONAL")])
    source = models.CharField(max_length=10, choices=[(s, s) for s in ("GENERATED", "TEMPLATE", "MANUAL")])
    position = models.PositiveIntegerField()
    archived_at = models.DateTimeField(null=True)
    generation_number = models.PositiveIntegerField(default=1)
    recruiter_edited = models.BooleanField(default=False)

    class Meta:
        ordering = ["position", "id"]

    def save(self, *args, **kwargs):
        if self.question_set.frozen_at:
            raise ValidationError("Frozen questions cannot change.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.question_set.frozen_at:
            raise ValidationError("Frozen questions must be preserved.")
        return super().delete(*args, **kwargs)


class ParsedResume(models.Model):
    """
    Output of the resume parsing/matching pipeline (Phase 5).

    NOTE: match_score / matched_skills / missing_skills are inherently
    job-relative, but this model is keyed 1:1 on Resume per the given
    schema. Treat these fields as the "most recent match" against
    whichever job triggered parsing. For true per-(resume, job) matching
    history, consider a separate ResumeJobMatch(resume, job) model later.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resume = models.OneToOneField(
        Resume, on_delete=models.CASCADE, related_name="parsed_data"
    )
    skills = models.JSONField(default=list)
    education = models.JSONField(default=list)
    experience = models.JSONField(default=list)
    certifications = models.JSONField(default=list)
    raw_text = models.TextField(blank=True)
    match_score = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    matched_skills = models.JSONField(default=list)
    missing_skills = models.JSONField(default=list)
    parsed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "parsed_resumes"
        verbose_name = "parsed resume"
        verbose_name_plural = "parsed resumes"

    def __str__(self):
        return f"ParsedResume({self.resume}) - score={self.match_score}"


# ═══════════════════════════════════════════════════════════════
#  Interview
# ═══════════════════════════════════════════════════════════════

class Interview(models.Model):
    """An interview scheduled/conducted for a given application."""

    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        LIVE = "LIVE", "Live"
        DONE = "DONE", "Done"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name="interviews"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.SCHEDULED
    )
    retell_session_id = models.CharField(max_length=255, null=True, blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    question_preparation_mode = models.CharField(max_length=24, default="LEGACY_UNVERIFIED",
        choices=[(s, s) for s in ("PREPARED", "DEMO_TEMPLATE", "LEGACY_UNVERIFIED")])
    question_fallback_reason = models.CharField(max_length=64, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completion_outcome = models.CharField(max_length=24, default="PENDING", choices=[(s, s) for s in
        ("PENDING", "USABLE", "FAILED", "INSUFFICIENT_EVIDENCE", "LEGACY_UNVERIFIED")])
    evaluation_status = models.CharField(max_length=16, default="NOT_STARTED", choices=[(s, s) for s in
        ("NOT_STARTED", "PENDING", "READY", "FAILED", "UNAVAILABLE")])
    question_set = models.ForeignKey(QuestionSet, null=True, blank=True, on_delete=models.PROTECT, related_name="interview_attempts")
    supersedes = models.OneToOneField('self', null=True, blank=True, on_delete=models.PROTECT, related_name='replacement_attempt')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "interviews"
        verbose_name = "interview"
        verbose_name_plural = "interviews"
        ordering = ["-scheduled_at"]
        indexes = [
            models.Index(fields=["status"], name="idx_interview_status"),
            models.Index(fields=["retell_session_id"], name="idx_interview_retell"),
        ]

    def __str__(self):
        return f"Interview({self.application}) - {self.status}"


# ═══════════════════════════════════════════════════════════════
#  Question
# ═══════════════════════════════════════════════════════════════

class Question(models.Model):
    """A single interview question, generated or templated."""

    class Category(models.TextChoices):
        BEHAVIORAL = "BEHAVIORAL", "Behavioral"
        TECHNICAL = "TECHNICAL", "Technical"
        SITUATIONAL = "SITUATIONAL", "Situational"

    class Source(models.TextChoices):
        GENERATED = "GENERATED", "Generated"
        TEMPLATE = "TEMPLATE", "Template"
        MANUAL = "MANUAL", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    interview = models.ForeignKey(
        Interview, on_delete=models.CASCADE, related_name="questions"
    )
    text = models.TextField()
    category = models.CharField(max_length=15, choices=Category.choices)
    source = models.CharField(max_length=10, choices=Source.choices)
    approved = models.BooleanField(default=False)
    position = models.PositiveIntegerField(default=0)
    prepared_question = models.ForeignKey(PreparedQuestion, null=True, blank=True, on_delete=models.PROTECT, related_name="frozen_copies")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "questions"
        verbose_name = "question"
        verbose_name_plural = "questions"
        ordering = ["position", "created_at", "pk"]
        indexes = [
            models.Index(fields=["category"], name="idx_question_category"),
            models.Index(fields=["approved"], name="idx_question_approved"),
        ]

    def __str__(self):
        return f"Q({self.category}): {self.text[:50]}"

    def save(self, *args, **kwargs):
        if self.interview.question_set_id:
            raise ValidationError("Scheduled questions are frozen copies; edit the draft before scheduling.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.interview.question_set_id:
            raise ValidationError("Scheduled question history must be preserved.")
        return super().delete(*args, **kwargs)


class TemplateQuestion(models.Model):
    """Generic fallback question used when job-specific generation fails."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    text = models.TextField()
    category = models.CharField(max_length=15, choices=Question.Category.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "template_questions"
        verbose_name = "template question"
        verbose_name_plural = "template questions"
        ordering = ["category", "created_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["text", "category"],
                name="uniq_template_question_text_category",
            ),
        ]

    def __str__(self):
        return f"Template Q({self.category}): {self.text[:50]}"


# ═══════════════════════════════════════════════════════════════
#  InterviewSession
# ═══════════════════════════════════════════════════════════════

class InterviewSession(models.Model):
    vision_calibration = models.JSONField(default=dict, blank=True)
    """The live/recorded session tied 1:1 to an interview."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    interview = models.OneToOneField(
        Interview, on_delete=models.CASCADE, related_name="session"
    )
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    audio_ended_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Verified Retell audio completion; ended_at records sealed video completion (or audio-only completion).',
    )
    transcript = models.TextField(blank=True)
    audio_terminal_status = models.CharField(max_length=16, blank=True, choices=[(s, s) for s in ("ended", "error", "not_connected")])
    video_required = models.BooleanField(default=True)
    creation_uncertain = models.BooleanField(default=False)
    # Migration leaves pre-B2 rows at version 0; only new sessions default to B2.
    lifecycle_version = models.PositiveSmallIntegerField(default=1)
    video_state = models.CharField(max_length=16, default='not_started', choices=[(s, s) for s in
        ('not_started', 'active', 'disconnected', 'finalizing', 'ended')])
    connection_state = models.CharField(max_length=16, default='not_connected', choices=[(s, s) for s in
        ('not_connected', 'connected', 'disconnected', 'closed')])
    analysis_state = models.CharField(max_length=16, default='pending', choices=[(s, s) for s in
        ('pending', 'processing', 'finalizing', 'complete', 'partial', 'failed')])
    analysis_failure_reason = models.CharField(max_length=32, blank=True, choices=[
        ('no_usable_evidence', 'No usable evidence'), ('pipeline_failed', 'Pipeline failed')])
    connection_generation = models.UUIDField(null=True, blank=True)
    connection_lease_until = models.DateTimeField(null=True, blank=True)
    connection_auth_expires_at = models.DateTimeField(null=True, blank=True)
    reconnect_deadline = models.DateTimeField(null=True, blank=True)
    video_activated_at = models.DateTimeField(null=True, blank=True)
    video_deadline = models.DateTimeField(null=True, blank=True)
    last_frame_at = models.DateTimeField(null=True, blank=True)
    last_sequence = models.PositiveBigIntegerField(null=True, blank=True)
    admission_sealed_at = models.DateTimeField(null=True, blank=True)
    sealed_admission_count = models.PositiveBigIntegerField(null=True, blank=True)
    finish_reason = models.CharField(max_length=24, blank=True, choices=[(s, s) for s in
        ('candidate_finished', 'audio_ended', 'reconnect_timeout', 'inactivity_timeout', 'duration_limit', 'service_failure')])
    received_count = models.PositiveBigIntegerField(default=0)
    admitted_count = models.PositiveBigIntegerField(default=0)
    dropped_count = models.PositiveBigIntegerField(default=0)
    analysis_ready_at = models.DateTimeField(null=True, blank=True)
    analysis_finalized_at = models.DateTimeField(null=True, blank=True)
    analysis_revision = models.PositiveBigIntegerField(default=0)
    analysis_source_revision = models.PositiveBigIntegerField(default=0)
    report_dirty = models.BooleanField(default=True)
    report_due_at = models.DateTimeField(default=timezone.now)
    finalization_token = models.UUIDField(null=True, blank=True)
    finalization_lease_until = models.DateTimeField(null=True, blank=True)
    recovery_due_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "interview_sessions"
        verbose_name = "interview session"
        verbose_name_plural = "interview sessions"
        indexes = [models.Index(fields=['report_due_at'], condition=models.Q(
            report_dirty=True, lifecycle_version=1, video_required=True), name='analysis_report_due')]
        constraints = [
            models.CheckConstraint(condition=models.Q(received_count=models.F('admitted_count') + models.F('dropped_count')),
                                   name='video_received_accounted'),
            models.CheckConstraint(condition=(models.Q(admission_sealed_at__isnull=True, sealed_admission_count__isnull=True)
                | models.Q(admission_sealed_at__isnull=False, sealed_admission_count__isnull=False)), name='video_seal_boundary_pair'),
        ]

    def __str__(self):
        return f"Session({self.interview})"


# ═══════════════════════════════════════════════════════════════
#  Response
# ═══════════════════════════════════════════════════════════════

class SocketTicket(models.Model):
    """Ephemeral connection credentials, never application identities or raw JWTs."""

    digest = models.CharField(primary_key=True, max_length=64, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    session = models.ForeignKey(InterviewSession, on_delete=models.CASCADE)
    purpose = models.CharField(max_length=9, choices=[('publisher', 'Publisher'), ('observer', 'Observer')])
    clerk_session_id = models.CharField(max_length=255, blank=True)
    clerk_subject = models.CharField(max_length=255)
    credential_expires_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True)

    class Meta:
        db_table = 'socket_tickets'


class RetellCall(models.Model):
    """One provider call attempt; browser credentials are encrypted at rest."""

    call_id = models.CharField(max_length=255, primary_key=True)
    session = models.ForeignKey(InterviewSession, on_delete=models.CASCADE, related_name='voice_calls')
    credentials = models.TextField(blank=True)
    expires_at = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=32, default='registered')
    disconnection_reason = models.CharField(max_length=255, blank=True)
    transcript = models.TextField(blank=True)
    transcript_object = models.JSONField(default=list, blank=True)
    recording_url = models.URLField(max_length=2048, blank=True)
    analysis = models.JSONField(default=dict, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)


class InterviewConversation(models.Model):
    """Versioned dialogue state, independent of media/evaluation completion."""
    interview = models.OneToOneField(Interview, primary_key=True, on_delete=models.PROTECT, related_name='conversation')
    call_id = models.CharField(max_length=255, unique=True)
    questions = models.JSONField(default=list)
    progress = models.JSONField(default=dict)
    carried_evidence = models.JSONField(default=dict)
    carried_hash = models.CharField(max_length=64, blank=True)
    cursor = models.PositiveIntegerField(default=0)
    revision = models.PositiveIntegerField(default=1)
    paused = models.BooleanField(default=False)
    end_kind = models.CharField(max_length=24, blank=True)
    stop_requested_at = models.DateTimeField(null=True)
    stop_confirmed_at = models.DateTimeField(null=True)
    stop_attempts = models.PositiveIntegerField(default=0)
    stop_lease_until = models.DateTimeField(null=True)
    next_stop_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            prior = type(self).objects.filter(pk=self.pk).values('questions', 'carried_evidence', 'carried_hash').first()
            if prior and any(prior[field] != getattr(self, field) for field in prior):
                raise ValidationError('Conversation question/evidence snapshots are immutable.')
        return super().save(*args, **kwargs)


class ConversationReceipt(models.Model):
    conversation = models.ForeignKey(InterviewConversation, on_delete=models.CASCADE, related_name='receipts')
    key = models.CharField(max_length=64)
    response = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Conversation receipts are immutable.')
        return super().save(*args, **kwargs)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['conversation', 'key'], name='conversation_request_once')]


class RetellWebhookEvent(models.Model):
    """Durable inbox; broker messages contain only this row's ID."""

    call_id = models.CharField(max_length=255)
    event = models.CharField(max_length=32, default='call_ended')
    content_hash = models.CharField(max_length=64, default='')
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['call_id', 'event', 'content_hash'], name='retell_event_content_once')]
        indexes = [models.Index(fields=['processed_at', 'next_attempt_at'], name='retell_inbox_pending')]


class TranscriptRevision(models.Model):
    """Immutable authenticated evidence; receipt order is not provider quality order."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    interview = models.ForeignKey(Interview, on_delete=models.PROTECT, related_name='transcript_revisions')
    number = models.PositiveIntegerField()
    call_id = models.CharField(max_length=255)
    source = models.CharField(max_length=24)
    content_hash = models.CharField(max_length=64)
    text = models.TextField(blank=True)
    turns = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['interview', 'number'], name='transcript_revision_number'),
                       models.UniqueConstraint(fields=['interview', 'call_id', 'content_hash'], name='transcript_content_once')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Transcript revisions are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Transcript evidence must be preserved.')


class EvaluationContext(models.Model):
    interview = models.OneToOneField(Interview, primary_key=True, on_delete=models.PROTECT, related_name='evaluation_context')
    snapshot = models.JSONField()
    content_hash = models.CharField(max_length=64)
    contract_version = models.CharField(max_length=32, default='answers-v1')
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Evaluation context is frozen.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Frozen evaluation context must be preserved.')


class TranscriptPreparation(models.Model):
    interview = models.OneToOneField(Interview, primary_key=True, on_delete=models.PROTECT, related_name='transcript_preparation')
    selected = models.ForeignKey(TranscriptRevision, null=True, on_delete=models.PROTECT)
    selected_source = models.CharField(max_length=24, blank=True)
    state = models.CharField(max_length=16, default='WAITING', choices=[(s, s) for s in ('WAITING', 'READY', 'UNAVAILABLE')])
    deadline = models.DateTimeField()
    next_sync_at = models.DateTimeField(default=timezone.now, db_index=True)
    sync_attempts = models.PositiveIntegerField(default=0)
    reason = models.CharField(max_length=64, blank=True)
    selection_version = models.PositiveIntegerField(default=1)
    selected_by = models.ForeignKey(User, null=True, on_delete=models.PROTECT, related_name='transcript_selections')
    selection_reason = models.TextField(blank=True)


class InterviewEvaluation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    interview = models.ForeignKey(Interview, on_delete=models.PROTECT, related_name='evaluations')
    revision = models.PositiveIntegerField()
    transcript = models.ForeignKey(TranscriptRevision, on_delete=models.PROTECT)
    context = models.ForeignKey(EvaluationContext, on_delete=models.PROTECT)
    supersedes = models.OneToOneField('self', null=True, on_delete=models.PROTECT, related_name='superseded_by')
    status = models.CharField(max_length=16, default='QUEUED', choices=[(s, s) for s in ('QUEUED', 'PROCESSING', 'READY', 'FAILED', 'UNAVAILABLE')])
    model_name = models.CharField(max_length=128)
    prompt_version = models.CharField(max_length=32, default='answers-v1')
    final_evidence = models.BooleanField(default=False)
    run_number = models.PositiveIntegerField(default=0)
    requested_by = models.ForeignKey(User, null=True, on_delete=models.PROTECT, related_name='evaluation_requests')
    request_reason = models.TextField(blank=True)
    score = models.FloatField(null=True)
    coverage = models.FloatField(null=True)
    error_code = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    claim_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    published_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['interview', 'revision'], name='evaluation_revision_once'),
                       models.UniqueConstraint(fields=['interview', 'transcript', 'context', 'final_evidence', 'run_number'], name='evaluation_evidence_run_once')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.get(pk=self.pk)
            frozen = ('interview_id', 'revision', 'transcript_id', 'context_id', 'supersedes_id', 'model_name', 'prompt_version', 'final_evidence', 'run_number', 'requested_by_id', 'request_reason')
            if any(getattr(previous, f) != getattr(self, f) for f in frozen) or previous.published_at:
                raise ValidationError('Evaluation inputs and published results are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Evaluation history must be preserved.')


class AnswerEvaluation(models.Model):
    evaluation = models.ForeignKey(InterviewEvaluation, on_delete=models.PROTECT, related_name='answers')
    question_id = models.UUIDField()
    result = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['evaluation', 'question_id'], name='answer_result_once')]

    def save(self, *args, **kwargs):
        if not self._state.adding or self.evaluation.published_at:
            raise ValidationError('Published answer results are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Answer evaluation history must be preserved.')


class EvaluationRelease(models.Model):
    evaluation = models.OneToOneField(InterviewEvaluation, primary_key=True, on_delete=models.PROTECT, related_name='feedback_release')
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Feedback releases are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Feedback release history must be preserved.')


class FeedbackReport(models.Model):
    """Frozen, allowlisted candidate projection; never a live evaluation serializer."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    evaluation = models.OneToOneField(InterviewEvaluation, on_delete=models.PROTECT, related_name='feedback_report')
    revision = models.PositiveIntegerField()
    contract_version = models.CharField(max_length=24, default='FEEDBACK_V1')
    status = models.CharField(max_length=16, default='PENDING', choices=[(s, s) for s in ('PENDING', 'READY', 'FAILED', 'UNAVAILABLE')])
    content = models.JSONField(default=dict)
    content_hash = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    prepared_at = models.DateTimeField(null=True)
    released_at = models.DateTimeField(null=True)
    released_by = models.ForeignKey(User, null=True, on_delete=models.PROTECT, related_name='released_feedback_reports')
    created_at = models.DateTimeField(auto_now_add=True)
    edit_version = models.PositiveIntegerField(default=1)
    amendments = models.JSONField(default=dict)
    amendments_hash = models.CharField(max_length=64, blank=True)
    retracted_at = models.DateTimeField(null=True)
    retracted_by = models.ForeignKey(User, null=True, on_delete=models.PROTECT, related_name='retracted_feedback')
    retraction_reason = models.TextField(blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=(models.Q(released_at__isnull=True, released_by__isnull=True)
            | (models.Q(released_at__isnull=False, released_by__isnull=False, prepared_at__isnull=False, status='READY')
               & ~models.Q(content_hash=''))), name='feedback_release_requires_prepared_report'),
            models.CheckConstraint(condition=(models.Q(retracted_at__isnull=True, retracted_by__isnull=True)
                | models.Q(retracted_at__isnull=False, retracted_by__isnull=False, released_at__isnull=False)), name='retraction_requires_release_actor')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = type(self).objects.get(pk=self.pk)
            immutable = ('evaluation_id', 'revision', 'contract_version')
            if any(getattr(old, f) != getattr(self, f) for f in immutable):
                raise ValidationError('Feedback identity is immutable.')
            if old.prepared_at and any(getattr(old, f) != getattr(self, f) for f in ('status', 'content', 'content_hash', 'error_code', 'prepared_at')):
                raise ValidationError('Prepared feedback is immutable; create a superseding evaluation.')
            if old.released_at and any(getattr(old, f) != getattr(self, f) for f in ('released_at', 'released_by_id')):
                raise ValidationError('Feedback releases are immutable.')
            if old.released_at and (old.amendments != self.amendments or old.amendments_hash != self.amendments_hash):
                raise ValidationError('Released public amendments are frozen.')
            if old.retracted_at and any(getattr(old, f) != getattr(self, f) for f in ('retracted_at', 'retracted_by_id', 'retraction_reason', 'edit_version')):
                raise ValidationError('Retraction history is immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Feedback report history must be preserved.')


class WorkflowAction(models.Model):
    """Append-only human commands and their idempotent resource/version identity."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.PROTECT, related_name='workflow_actions')
    interview = models.ForeignKey(Interview, null=True, on_delete=models.PROTECT)
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    action = models.CharField(max_length=48)
    expected_version = models.PositiveIntegerField()
    reason = models.TextField()
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['application', 'action', 'expected_version'], name='workflow_command_once')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Workflow action history is immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Workflow action history must be preserved.')


class AnswerExtraction(models.Model):
    evaluation = models.OneToOneField(InterviewEvaluation, primary_key=True, on_delete=models.PROTECT, related_name='extraction')
    contract_version = models.CharField(max_length=24, default='ALIGNMENT_V1')
    input_hash = models.CharField(max_length=64)
    content_hash = models.CharField(max_length=64)
    content = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Answer extraction is immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Answer evidence must be preserved.')


class FeedbackChange(models.Model):
    report = models.ForeignKey(FeedbackReport, on_delete=models.PROTECT, related_name='changes')
    version = models.PositiveIntegerField()
    action = models.CharField(max_length=16)
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    reason = models.TextField()
    content = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['report', 'version'], name='feedback_change_once')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Feedback change history is immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Feedback changes must be preserved.')


class Response(models.Model):
    """A candidate's answer to a question within a session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="responses"
    )
    session = models.ForeignKey(
        InterviewSession, on_delete=models.CASCADE, related_name="responses"
    )
    answer_text = models.TextField(blank=True)
    evaluation = models.ForeignKey(InterviewEvaluation, null=True, on_delete=models.PROTECT, related_name='extracted_responses')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "responses"
        verbose_name = "response"
        verbose_name_plural = "responses"
        ordering = ["created_at"]
        constraints = [models.UniqueConstraint(fields=['evaluation', 'question'], name='response_evaluation_question_once')]
        indexes = [
            models.Index(fields=["session"], name="idx_response_session"),
        ]

    def __str__(self):
        return f"Response(q={self.question_id}, session={self.session_id})"

    def save(self, *args, **kwargs):
        if self.evaluation_id and (not self._state.adding or self.evaluation.published_at):
            raise ValidationError('Extracted answer history is immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.evaluation_id:
            raise ValidationError('Extracted answer history must be preserved.')
        return super().delete(*args, **kwargs)


# ═══════════════════════════════════════════════════════════════
#  BehavioralAnalysis
# ═══════════════════════════════════════════════════════════════

class BehavioralAnalysis(models.Model):
    """Aggregate behavioral analysis for an entire session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField(
        InterviewSession, on_delete=models.CASCADE, related_name="behavioral_analysis"
    )
    attention_pct = models.FloatField(null=True, blank=True)
    look_away_count = models.PositiveIntegerField(default=0, null=True, blank=True)
    integrity_flags = models.JSONField(default=list)
    emotion_summary = models.JSONField(default=dict)
    # Blank/zero provenance is legacy or unavailable, never evidence of real ML.
    pipeline_version = models.CharField(max_length=100, blank=True)
    model_version = models.CharField(max_length=100, blank=True)
    rules_version = models.CharField(max_length=100, blank=True)
    revision = models.PositiveBigIntegerField(default=0)
    finalized_at = models.DateTimeField(null=True, blank=True)
    sealed_admission_count = models.PositiveBigIntegerField(null=True, blank=True)
    coverage = models.JSONField(default=dict)
    report_schema_version = models.PositiveSmallIntegerField(default=0)
    report = models.JSONField(default=dict, blank=True)
    source_revision = models.PositiveBigIntegerField(default=0)
    notified_revision = models.PositiveBigIntegerField(default=0)
    notify_after = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "behavioral_analyses"
        verbose_name = "behavioral analysis"
        verbose_name_plural = "behavioral analyses"

    def __str__(self):
        return f"BehavioralAnalysis({self.session})"


# ═══════════════════════════════════════════════════════════════
#  FrameAnalysis
# ═══════════════════════════════════════════════════════════════

class FrameAdmissionGate(models.Model):
    """Single global capacity lock. Lock before any interview/session rows."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(pk=1), name='frame_gate_singleton')]


class FrameJob(models.Model):
    """Admitted metadata only. Pending jobs are not FrameAnalysis observations."""
    class State(models.TextChoices):
        PENDING = 'pending'
        PROCESSING = 'processing'
        PROCESSED = 'processed'
        SUPERSEDED = 'superseded'
        FAILED = 'failed'
        EXPIRED = 'expired'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(InterviewSession, on_delete=models.CASCADE, related_name='frame_jobs')
    generation = models.UUIDField()
    sequence = models.PositiveBigIntegerField()
    admission_number = models.PositiveBigIntegerField()
    capture_ts = models.FloatField()
    received_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    state = models.CharField(max_length=12, choices=State.choices, default=State.PENDING)
    claim_token = models.UUIDField(null=True, blank=True)
    claim_expires_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    terminal_at = models.DateTimeField(null=True, blank=True)
    terminal_reason = models.CharField(max_length=32, blank=True)
    payload_ready = models.BooleanField(default=False)
    publication_token = models.UUIDField(null=True, blank=True)
    publication_attempts = models.PositiveSmallIntegerField(default=0)
    publication_due_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['session', 'generation', 'sequence'], name='frame_identity_once'),
            models.UniqueConstraint(fields=['session', 'admission_number'], name='frame_admission_once'),
            models.UniqueConstraint(fields=['session'], condition=models.Q(state='pending'), name='one_pending_frame'),
            models.UniqueConstraint(fields=['session'], condition=models.Q(state='processing'), name='one_processing_frame'),
            models.CheckConstraint(condition=(models.Q(state__in=['pending', 'processing'], terminal_at__isnull=True)
                | models.Q(state__in=['processed', 'superseded', 'failed', 'expired'], terminal_at__isnull=False)),
                name='frame_terminal_time'),
            models.CheckConstraint(condition=(~models.Q(state='processing')
                | models.Q(claim_token__isnull=False, claim_expires_at__isnull=False)), name='frame_processing_claim'),
        ]
        indexes = [models.Index(fields=['state', 'expires_at'], name='frame_job_expiry')]


class FrameAnalysis(models.Model):
    """Per-frame analysis captured during a session (many per session)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        InterviewSession, on_delete=models.CASCADE, related_name="frame_analyses"
    )
    ts = models.DateTimeField()
    gaze_direction = models.CharField(max_length=50, blank=True)
    face_count = models.PositiveIntegerField(default=0, null=True, blank=True)
    detected_objects = models.JSONField(default=list)
    emotion = models.CharField(max_length=50, null=True, blank=True)
    job = models.OneToOneField(FrameJob, null=True, blank=True, on_delete=models.PROTECT, related_name='observation')
    pipeline_version = models.CharField(max_length=100, blank=True)
    model_version = models.CharField(max_length=100, blank=True)
    rules_version = models.CharField(max_length=100, blank=True)
    vision_details = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "frame_analyses"
        verbose_name = "frame analysis"
        verbose_name_plural = "frame analyses"
        ordering = ["ts"]
        indexes = [
            models.Index(fields=["session", "ts"], name="idx_frame_session_ts"),
        ]

    def __str__(self):
        return f"Frame({self.session_id}) @ {self.ts}"


# ═══════════════════════════════════════════════════════════════
#  ScoringRubric
# ═══════════════════════════════════════════════════════════════

class ScoringRubric(models.Model):
    """A configurable weighting scheme for computing candidate scores."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    weight_match = models.FloatField(default=0.4)
    weight_interview = models.FloatField(default=0.6)
    weight_behavioral = models.FloatField(default=0)
    active = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=0)
    policy_version = models.CharField(max_length=24, default='COMBINED_V1')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "scoring_rubrics"
        verbose_name = "scoring rubric"
        verbose_name_plural = "scoring rubrics"
        indexes = [
            models.Index(fields=["active"], name="idx_rubric_active"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["active"],
                condition=models.Q(active=True),
                name="uniq_active_scoring_rubric",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({'active' if self.active else 'inactive'})"

    def save(self, *args, **kwargs):
        from django.db import transaction
        from ai.scoring.rubric import combined_score
        if self.policy_version not in ('COMBINED_V1', 'LEGACY_V0'):
            raise ValidationError('Unknown scoring policy version.')
        if self.policy_version == 'COMBINED_V1':
            try:
                combined_score(None, None, weight_match=self.weight_match,
                    weight_interview=self.weight_interview, weight_behavioral=self.weight_behavioral)
            except (ValueError, ArithmeticError) as exc:
                raise ValidationError(str(exc)) from None
        if not self._state.adding:
            old = type(self).objects.get(pk=self.pk)
            if any(getattr(old, f) != getattr(self, f) for f in
                   ('name', 'weight_match', 'weight_interview', 'weight_behavioral', 'version', 'policy_version')):
                raise ValidationError('Rubric inputs are immutable. Create and activate a new rubric version.')
        with transaction.atomic():
            state, _ = ScoringPolicyState.objects.get_or_create(pk=1)
            state = ScoringPolicyState.objects.select_for_update().get(pk=1)
            if self._state.adding:
                self.version = state.next_version
                state.next_version += 1
            super().save(*args, **kwargs)
            if self.active and state.active_rubric_id != self.pk:
                state.active_rubric = self
                state.rebuild_pending, state.rebuild_cursor = True, None
                state.epoch += 1
            elif not self.active and state.active_rubric_id == self.pk:
                state.active_rubric = None
                state.epoch += 1
            state.save()


class ScoringPolicyState(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    active_rubric = models.ForeignKey(ScoringRubric, null=True, on_delete=models.PROTECT)
    next_version = models.PositiveIntegerField(default=1)
    epoch = models.PositiveIntegerField(default=0)
    rebuild_pending = models.BooleanField(default=False)
    rebuild_cursor = models.UUIDField(null=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name='scoring_policy_singleton')]


class ScoreRevision(models.Model):
    """Durable job plus immutable historical result; CandidateScore is a projection."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.PROTECT, related_name='score_revisions')
    number = models.PositiveIntegerField()
    key = models.CharField(max_length=64, unique=True)
    assessment = models.ForeignKey(ApplicationAssessment, null=True, on_delete=models.PROTECT)
    evaluation = models.ForeignKey(InterviewEvaluation, null=True, on_delete=models.PROTECT)
    rubric = models.ForeignKey(ScoringRubric, null=True, on_delete=models.PROTECT)
    inputs = models.JSONField()
    status = models.CharField(max_length=16, default='QUEUED', choices=[(s, s) for s in
        ('QUEUED', 'PROCESSING', 'COMPLETE', 'INCOMPLETE', 'FAILED', 'STALE', 'LEGACY')])
    final_score = models.DecimalField(max_digits=5, decimal_places=2, null=True)
    screening_match_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True)
    interview_performance_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True)
    coverage = models.DecimalField(max_digits=7, decimal_places=6, default=0)
    error_code = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    claim_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['application', 'number'], name='score_revision_number'),
            models.CheckConstraint(condition=~models.Q(status='COMPLETE') | models.Q(
                final_score__isnull=False, final_score__gte=0, final_score__lte=100, coverage=1,
                completed_at__isnull=False, assessment__isnull=False, evaluation__isnull=False, rubric__isnull=False),
                name='complete_score_requires_evidence')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = type(self).objects.get(pk=self.pk)
            if old.completed_at or old.status in ('LEGACY', 'INCOMPLETE') or any(getattr(old, f) != getattr(self, f) for f in
                    ('application_id', 'number', 'key', 'assessment_id', 'evaluation_id', 'rubric_id', 'inputs')):
                raise ValidationError('Score inputs and completed score history are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Score history must be preserved.')


# ═══════════════════════════════════════════════════════════════
#  CandidateScore
# ═══════════════════════════════════════════════════════════════

class CandidateScore(models.Model):
    """Final computed score for an application."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.OneToOneField(
        Application, on_delete=models.CASCADE, related_name="score"
    )
    final_score = models.FloatField(null=True, blank=True)
    current_revision = models.ForeignKey(ScoreRevision, null=True, on_delete=models.PROTECT, related_name='projections')
    updated_at = models.DateTimeField(auto_now=True)
    breakdown = models.JSONField(default=dict)
    explanation = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "candidate_scores"
        verbose_name = "candidate score"
        verbose_name_plural = "candidate scores"

    def __str__(self):
        return f"Score({self.application}) = {self.final_score}"


# ═══════════════════════════════════════════════════════════════
#  Notification
# ═══════════════════════════════════════════════════════════════

class Notification(models.Model):
    """A notification delivered to a user."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="notifications"
    )
    message = models.TextField()
    type = models.CharField(max_length=50)
    links = models.JSONField(default=dict)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notifications"
        verbose_name = "notification"
        verbose_name_plural = "notifications"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "is_read"], name="idx_notif_user_read"),
            models.Index(fields=["type"], name="idx_notif_type"),
        ]

    def __str__(self):
        return f"Notification({self.user}) - {self.type}"


class NotificationOutbox(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(User, on_delete=models.PROTECT, related_name='notification_intents')
    event = models.CharField(max_length=50)
    resource_type = models.CharField(max_length=32)
    resource_id = models.UUIDField()
    revision = models.CharField(max_length=64)
    business_key = models.CharField(max_length=64, unique=True)
    links = models.JSONField(default=dict)
    inapp_delivered_at = models.DateTimeField(null=True)
    email_status = models.CharField(max_length=16, default='PENDING', choices=[(s, s) for s in ('PENDING', 'SENDING', 'SENT', 'FAILED', 'SKIPPED')])
    email_attempts = models.PositiveIntegerField(default=0)
    delivery_generation = models.PositiveIntegerField(default=1)
    total_email_attempts = models.PositiveIntegerField(default=0)
    claim_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    email_sent_at = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['recipient', 'event', 'resource_type', 'resource_id', 'revision'],
                                              name='notification_business_event_once')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = type(self).objects.get(pk=self.pk)
            if any(getattr(old, f) != getattr(self, f) for f in
                    ('recipient_id', 'event', 'resource_type', 'resource_id', 'revision', 'business_key', 'links')):
                raise ValidationError('Notification business identity is immutable.')
        return super().save(*args, **kwargs)


# ═══════════════════════════════════════════════════════════════
#  AuditLogEntry
# ═══════════════════════════════════════════════════════════════

class NotificationRetry(models.Model):
    outbox = models.ForeignKey(NotificationOutbox, on_delete=models.PROTECT, related_name='operator_retries')
    generation = models.PositiveIntegerField()
    previous_attempts = models.PositiveIntegerField()
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['outbox', 'generation'], name='notification_retry_generation_once')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Notification retry audit is immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Notification retry history must be preserved.')


class IntegrityThresholds(models.Model):
    """Display-only configuration; the database permits only singleton key 1."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    look_away_limit = models.PositiveIntegerField(default=5)
    integrity_flag_limit = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "integrity_thresholds"
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="integrity_thresholds_singleton")]


class AuditLogEntry(models.Model):
    """Immutable audit trail entry for system/user actions."""
    business_key = models.CharField(max_length=64, null=True, unique=True)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_entries",
    )
    action = models.CharField(max_length=100)
    target_type = models.CharField(max_length=100)
    target_id = models.CharField(max_length=64)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_log_entries"
        verbose_name = "audit log entry"
        verbose_name_plural = "audit log entries"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["target_type", "target_id"], name="idx_audit_target"),
            models.Index(fields=["action"], name="idx_audit_action"),
        ]

    def __str__(self):
        return f"AuditLog({self.action} on {self.target_type}:{self.target_id})"
