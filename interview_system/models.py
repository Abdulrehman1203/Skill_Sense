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
    is_active = models.BooleanField(
        default=True,
        help_text="Soft-disable flag for admin deactivation (FR-06). Not a Django auth flag.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "users"
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
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    deadline = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "jobs"
        verbose_name = "job"
        verbose_name_plural = "jobs"
        ordering = ["-created_at"]
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
        validators=[FileExtensionValidator(allowed_extensions=["pdf", "doc", "docx"])],
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        help_text="Pipeline state: PENDING → PARSED or FAILED.",
    )
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
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.APPLIED
    )
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


# ═══════════════════════════════════════════════════════════════
#  InterviewSession
# ═══════════════════════════════════════════════════════════════

class InterviewSession(models.Model):
    """The live/recorded session tied 1:1 to an interview."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    interview = models.OneToOneField(
        Interview, on_delete=models.CASCADE, related_name="session"
    )
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    transcript = models.TextField(blank=True)

    class Meta:
        db_table = "interview_sessions"
        verbose_name = "interview session"
        verbose_name_plural = "interview sessions"

    def __str__(self):
        return f"Session({self.interview})"


# ═══════════════════════════════════════════════════════════════
#  Response
# ═══════════════════════════════════════════════════════════════

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
    look_away_count = models.PositiveIntegerField(default=0)
    integrity_flags = models.JSONField(default=list)
    emotion_summary = models.JSONField(default=dict)
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

class FrameAnalysis(models.Model):
    """Per-frame analysis captured during a session (many per session)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        InterviewSession, on_delete=models.CASCADE, related_name="frame_analyses"
    )
    ts = models.DateTimeField()
    gaze_direction = models.CharField(max_length=50, blank=True)
    face_count = models.PositiveIntegerField(default=0)
    detected_objects = models.JSONField(default=list)
    emotion = models.CharField(max_length=50, null=True, blank=True)

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
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "scoring_rubrics"
        verbose_name = "scoring rubric"
        verbose_name_plural = "scoring rubrics"
        indexes = [
            models.Index(fields=["active"], name="idx_rubric_active"),
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

