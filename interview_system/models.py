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

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    interview = models.ForeignKey(
        Interview, on_delete=models.CASCADE, related_name="questions"
    )
    text = models.TextField()
    category = models.CharField(max_length=15, choices=Category.choices)
    source = models.CharField(max_length=10, choices=Source.choices)
    approved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "questions"
        verbose_name = "question"
        verbose_name_plural = "questions"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["category"], name="idx_question_category"),
            models.Index(fields=["approved"], name="idx_question_approved"),
        ]

    def __str__(self):
        return f"Q({self.category}): {self.text[:50]}"


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


class RetellWebhookEvent(models.Model):
    """Durable inbox; broker messages contain only this row's ID."""

    call_id = models.CharField(max_length=255)
    event = models.CharField(max_length=32, default='call_ended')
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['call_id', 'event'], name='retell_event_once')]
        indexes = [models.Index(fields=['processed_at', 'next_attempt_at'], name='retell_inbox_pending')]


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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "responses"
        verbose_name = "response"
        verbose_name_plural = "responses"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["session"], name="idx_response_session"),
        ]

    def __str__(self):
        return f"Response(q={self.question_id}, session={self.session_id})"


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
    weight_match = models.FloatField(default=0)
    weight_interview = models.FloatField(default=0)
    weight_behavioral = models.FloatField(default=0)
    active = models.BooleanField(default=False)
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


# ═══════════════════════════════════════════════════════════════
#  AuditLogEntry
# ═══════════════════════════════════════════════════════════════

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
