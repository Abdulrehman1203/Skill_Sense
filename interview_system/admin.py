"""
Admin configuration for the interview_system app.

Registers all interview_system models in the Django admin
with sensible column displays, filters, and inlines.
"""

from django.contrib import admin

from .models import (
    Application,
    AuditLogEntry,
    BehavioralAnalysis,
    CandidateProfile,
    CandidateScore,
    FrameAnalysis,
    Interview,
    InterviewSession,
    Job,
    JobSkill,
    Notification,
    ParsedResume,
    Question,
    RecruiterProfile,
    Response,
    Resume,
    ScoringRubric,
    User,
)


# ── Inline profiles ──────────────────────────────────────────

class RecruiterProfileInline(admin.StackedInline):
    model = RecruiterProfile
    can_delete = False
    verbose_name = "Recruiter Profile"
    verbose_name_plural = "Recruiter Profile"


class CandidateProfileInline(admin.StackedInline):
    model = CandidateProfile
    can_delete = False
    verbose_name = "Candidate Profile"
    verbose_name_plural = "Candidate Profile"


class JobSkillInline(admin.TabularInline):
    model = JobSkill
    extra = 1


class ParsedResumeInline(admin.StackedInline):
    model = ParsedResume
    extra = 0


class QuestionInline(admin.TabularInline):
    model = Question
    extra = 0


class ResponseInline(admin.TabularInline):
    model = Response
    extra = 0


# ── User Admin ───────────────────────────────────────────────

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    """Admin view for the Clerk-based User record."""

    list_display = ("email", "clerk_id", "role", "is_active", "created_at", "updated_at")
    list_filter = ("role", "is_active")
    search_fields = ("email", "clerk_id", "first_name", "last_name")
    ordering = ("-created_at",)
    readonly_fields = ("id", "created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("id", "clerk_id", "email")}),
        ("Personal Info", {"fields": ("first_name", "last_name")}),
        ("Role & Status", {"fields": ("role", "is_active")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    inlines = [RecruiterProfileInline, CandidateProfileInline]


# ── Standalone profile admins ───────────────────────────────

@admin.register(RecruiterProfile)
class RecruiterProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "company_name", "industry", "position", "company_size")
    search_fields = ("user__email", "company_name")


@admin.register(CandidateProfile)
class CandidateProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "headline", "location", "years_experience")
    search_fields = ("user__email", "headline")


# ── Job Admin ────────────────────────────────────────────────

@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("title", "recruiter", "status", "job_type", "experience_level", "created_at")
    list_filter = ("status", "job_type", "experience_level")
    search_fields = ("title", "skills_required")
    inlines = [JobSkillInline]


@admin.register(JobSkill)
class JobSkillAdmin(admin.ModelAdmin):
    list_display = ("skill_name", "job", "is_required")
    list_filter = ("is_required",)
    search_fields = ("skill_name",)


# ── Resume / Application Admin ──────────────────────────────

@admin.register(Resume)
class ResumeAdmin(admin.ModelAdmin):
    list_display = ("candidate", "status", "uploaded_at")
    list_filter = ("status",)
    inlines = [ParsedResumeInline]


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ("candidate", "job", "status", "created_at")
    list_filter = ("status", "job")
    search_fields = ("candidate__user__email", "job__title")


@admin.register(ParsedResume)
class ParsedResumeAdmin(admin.ModelAdmin):
    list_display = ("resume", "match_score", "parsed_at")


# ── Interview Admin ──────────────────────────────────────────

@admin.register(Interview)
class InterviewAdmin(admin.ModelAdmin):
    list_display = ("application", "status", "scheduled_at", "retell_session_id")
    list_filter = ("status",)
    search_fields = ("retell_session_id",)
    inlines = [QuestionInline]


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("interview", "category", "source", "approved")
    list_filter = ("category", "source", "approved")
    search_fields = ("text",)


@admin.register(InterviewSession)
class InterviewSessionAdmin(admin.ModelAdmin):
    list_display = ("interview", "started_at", "ended_at")
    inlines = [ResponseInline]


@admin.register(Response)
class ResponseAdmin(admin.ModelAdmin):
    list_display = ("question", "session", "created_at")
    search_fields = ("answer_text",)


# ── Analysis / Scoring Admin ─────────────────────────────────

@admin.register(BehavioralAnalysis)
class BehavioralAnalysisAdmin(admin.ModelAdmin):
    list_display = ("session", "attention_pct", "look_away_count")


@admin.register(FrameAnalysis)
class FrameAnalysisAdmin(admin.ModelAdmin):
    list_display = ("session", "ts", "gaze_direction", "face_count", "emotion")
    list_filter = ("gaze_direction", "emotion")


@admin.register(ScoringRubric)
class ScoringRubricAdmin(admin.ModelAdmin):
    list_display = ("name", "weight_match", "weight_interview", "weight_behavioral", "active")
    list_filter = ("active",)


@admin.register(CandidateScore)
class CandidateScoreAdmin(admin.ModelAdmin):
    list_display = ("application", "final_score", "created_at")


# ── Notification / Audit Admin ───────────────────────────────

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "type", "is_read", "created_at")
    list_filter = ("type", "is_read")


@admin.register(AuditLogEntry)
class AuditLogEntryAdmin(admin.ModelAdmin):
    list_display = ("actor", "action", "target_type", "target_id", "created_at")
    list_filter = ("action", "target_type")
    search_fields = ("target_id",)
