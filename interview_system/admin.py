"""
Admin configuration for the interview_system app.

Registers User, RecruiterProfile, and CandidateProfile models
in the Django admin with sensible column displays and filters.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import CandidateProfile, RecruiterProfile, User


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


# ── User Admin ───────────────────────────────────────────────

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """Custom admin view for the email-based User model."""

    list_display = ("email", "role", "is_active", "is_verified", "is_staff", "created_at")
    list_filter = ("role", "is_active", "is_verified", "is_staff")
    search_fields = ("email",)
    ordering = ("-created_at",)

    # Override fieldsets because we don't have a username field
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Role & Status", {"fields": ("role", "is_active", "is_verified")}),
        ("Permissions", {"fields": ("is_staff", "is_superuser", "groups", "user_permissions")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "role", "password1", "password2"),
            },
        ),
    )

    inlines = [RecruiterProfileInline, CandidateProfileInline]


# ── Standalone profile admins (optional, for direct access) ──

@admin.register(RecruiterProfile)
class RecruiterProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "company_name", "industry", "position", "company_size")
    search_fields = ("user__email", "company_name")


@admin.register(CandidateProfile)
class CandidateProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "headline", "location", "years_experience")
    search_fields = ("user__email", "headline")
