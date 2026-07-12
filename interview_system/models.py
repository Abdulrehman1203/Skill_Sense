"""
Core data models for the SkillSense interview/recruitment platform.

Models:
    User              – Custom auth user (email-based, with role).
    RecruiterProfile  – Extended profile for RECRUITER users.
    CandidateProfile  – Extended profile for CANDIDATE users.
"""

from __future__ import annotations

from typing import ClassVar

import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

from .managers import CustomUserManager


# ═══════════════════════════════════════════════════════════════
#  User
# ═══════════════════════════════════════════════════════════════

class User(AbstractBaseUser, PermissionsMixin):
    """
    Custom user model that uses **email** as the unique identifier
    instead of Django's default username.
    """

    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Admin"
        RECRUITER = "RECRUITER", "Recruiter"
        CANDIDATE = "CANDIDATE", "Candidate"

    id: models.UUIDField = models.UUIDField(  # pyrefly: ignore[bad-override-mutable-attribute]
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    email: models.EmailField = models.EmailField(
        unique=True,
        max_length=255,
        verbose_name="email address",
    )
    first_name: models.CharField = models.CharField(max_length=150, blank=True)
    last_name: models.CharField = models.CharField(max_length=150, blank=True)
    role: models.CharField = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.CANDIDATE,
    )
    is_active: models.BooleanField = models.BooleanField(default=True)  # pyrefly: ignore[bad-override-mutable-attribute]
    is_verified: models.BooleanField = models.BooleanField(
        default=False,
        help_text="Indicates whether the user has verified their email.",
    )
    is_staff: models.BooleanField = models.BooleanField(
        default=False,
        help_text="Designates whether the user can log into the admin site.",
    )
    created_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)

    objects: ClassVar[CustomUserManager] = CustomUserManager()

    USERNAME_FIELD: ClassVar[str] = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        db_table: str = "users"
        verbose_name: str = "user"
        verbose_name_plural: str = "users"
        ordering: list[str] = ["-created_at"]

    def __str__(self) -> str:
        return self.email


# ═══════════════════════════════════════════════════════════════
#  RecruiterProfile
# ═══════════════════════════════════════════════════════════════

class RecruiterProfile(models.Model):
    """One-to-one profile attached to every RECRUITER user."""

    user: models.OneToOneField = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="recruiter_profile",
        primary_key=True,
    )
    phone: models.CharField = models.CharField(max_length=20, blank=True, default="")
    company_name: models.CharField = models.CharField(max_length=255, blank=True, default="")
    company_logo: models.ImageField = models.ImageField(
        upload_to="recruiter/logos/",
        blank=True,
        null=True,
    )
    industry: models.CharField = models.CharField(max_length=150, blank=True, default="")
    position: models.CharField = models.CharField(max_length=150, blank=True, default="")
    company_size: models.CharField = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        db_table: str = "recruiter_profiles"
        verbose_name: str = "recruiter profile"
        verbose_name_plural: str = "recruiter profiles"

    def __str__(self) -> str:
        return f"{self.user.email} – {self.company_name or 'No Company'}"


# ═══════════════════════════════════════════════════════════════
#  CandidateProfile
# ═══════════════════════════════════════════════════════════════

class CandidateProfile(models.Model):
    """One-to-one profile attached to every CANDIDATE user."""

    user: models.OneToOneField = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="candidate_profile",
        primary_key=True,
    )
    phone: models.CharField = models.CharField(max_length=20, blank=True, default="")
    location: models.CharField = models.CharField(max_length=255, blank=True, default="")
    linkedin_url: models.URLField = models.URLField(blank=True, default="")
    portfolio_url: models.URLField = models.URLField(blank=True, default="")
    years_experience: models.PositiveIntegerField = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text="Total years of professional experience.",
    )
    headline: models.CharField = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="A short professional headline (e.g. 'Senior Python Developer').",
    )

    class Meta:
        db_table: str = "candidate_profiles"
        verbose_name: str = "candidate profile"
        verbose_name_plural: str = "candidate profiles"

    def __str__(self) -> str:
        return f"{self.user.email} – {self.headline or 'No Headline'}"
