"""
Serializers for authentication — registration and login.

Covers:
    RecruiterRegisterSerializer  – validates + creates User(role=RECRUITER) + RecruiterProfile
    CandidateRegisterSerializer  – validates + creates User(role=CANDIDATE) + CandidateProfile
    LoginSerializer              – validates email/password credentials
"""

from __future__ import annotations

from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import CandidateProfile, RecruiterProfile, User


# ══════════════════════════════════════════════════════════════
#  Shared helpers
# ══════════════════════════════════════════════════════════════

class PasswordMixin(serializers.Serializer):
    """
    Provides validated password + confirm-password fields.
    Inject into any registration serializer.
    """

    password = serializers.CharField(
        write_only=True,
        min_length=8,
        style={"input_type": "password"},
        validators=[validate_password],
    )
    password_confirm = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
    )

    def validate(self, attrs: dict) -> dict:
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError(
                {"password_confirm": "Passwords do not match."}
            )
        return attrs


# ══════════════════════════════════════════════════════════════
#  Recruiter Registration
# ══════════════════════════════════════════════════════════════

class RecruiterRegisterSerializer(PasswordMixin, serializers.ModelSerializer):
    """
    Register a new RECRUITER user together with their profile.
    All profile fields are optional at sign-up.
    """

    first_name = serializers.CharField(max_length=150, required=True)
    last_name = serializers.CharField(max_length=150, required=True)
    phone = serializers.CharField(max_length=20, required=True)

    # Profile fields (company_name required, rest optional)
    company_name = serializers.CharField(max_length=255, required=True)
    industry = serializers.CharField(max_length=150, required=False, default="")
    position = serializers.CharField(max_length=150, required=False, default="")
    company_size = serializers.CharField(max_length=50, required=False, default="")

    class Meta:  # pyrefly: ignore[bad-override]
        model = User
        fields = [
            "first_name",
            "last_name",
            "email",
            "password",
            "password_confirm",
            "phone",
            "company_name",
            "industry",
            "position",
            "company_size",
        ]

    def create(self, validated_data: dict) -> User:
        # Pull out profile-specific + confirm fields
        profile_fields = {
            "phone": validated_data.pop("phone", ""),
            "company_name": validated_data.pop("company_name", ""),
            "industry": validated_data.pop("industry", ""),
            "position": validated_data.pop("position", ""),
            "company_size": validated_data.pop("company_size", ""),
        }
        validated_data.pop("password_confirm")

        user = User.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            first_name=validated_data["first_name"],
            last_name=validated_data["last_name"],
            role=User.Role.RECRUITER,
        )
        RecruiterProfile.objects.create(user=user, **profile_fields)
        return user


# ══════════════════════════════════════════════════════════════
#  Candidate Registration
# ══════════════════════════════════════════════════════════════

class CandidateRegisterSerializer(PasswordMixin, serializers.ModelSerializer):
    """
    Register a new CANDIDATE user together with their profile.
    All profile fields are optional at sign-up.
    """

    first_name = serializers.CharField(max_length=150, required=True)
    last_name = serializers.CharField(max_length=150, required=True)
    phone = serializers.CharField(max_length=20, required=True)

    # Profile fields (all optional at registration)
    location = serializers.CharField(max_length=255, required=False, default="")
    linkedin_url = serializers.URLField(required=False, default="")
    portfolio_url = serializers.URLField(required=False, default="")
    years_experience = serializers.IntegerField(min_value=0, required=False, allow_null=True, default=None)
    headline = serializers.CharField(max_length=255, required=False, default="")

    class Meta:  # pyrefly: ignore[bad-override]
        model = User
        fields = [
            "first_name",
            "last_name",
            "email",
            "password",
            "password_confirm",
            "phone",
            "location",
            "linkedin_url",
            "portfolio_url",
            "years_experience",
            "headline",
        ]

    def create(self, validated_data: dict) -> User:
        profile_fields = {
            "phone": validated_data.pop("phone", ""),
            "location": validated_data.pop("location", ""),
            "linkedin_url": validated_data.pop("linkedin_url", ""),
            "portfolio_url": validated_data.pop("portfolio_url", ""),
            "years_experience": validated_data.pop("years_experience", None),
            "headline": validated_data.pop("headline", ""),
        }
        validated_data.pop("password_confirm")

        user = User.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            first_name=validated_data["first_name"],
            last_name=validated_data["last_name"],
            role=User.Role.CANDIDATE,
        )
        CandidateProfile.objects.create(user=user, **profile_fields)
        return user


# ══════════════════════════════════════════════════════════════
#  Login
# ══════════════════════════════════════════════════════════════

class LoginSerializer(serializers.Serializer):
    """
    Validate email + password credentials.
    Returns the authenticated User instance via .validated_data["user"].
    """

    email = serializers.EmailField()
    password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
    )

    def validate(self, attrs: dict) -> dict:
        user = authenticate(
            request=self.context.get("request"),
            username=attrs["email"],   # Django's authenticate maps USERNAME_FIELD internally
            password=attrs["password"],
        )
        if user is None:
            raise serializers.ValidationError(
                {"non_field_errors": "Invalid email or password."}
            )
        if not user.is_active:
            raise serializers.ValidationError(
                {"non_field_errors": "This account has been deactivated."}
            )
        attrs["user"] = user
        return attrs


# ══════════════════════════════════════════════════════════════
#  Response serializers (read-only, for consistent API output)
# ══════════════════════════════════════════════════════════════

class UserResponseSerializer(serializers.ModelSerializer):
    """Minimal user payload returned after register / login."""

    class Meta:  # pyrefly: ignore[bad-override]
        model = User
        fields = ["id", "email", "first_name", "last_name", "role", "is_verified", "created_at"]
        read_only_fields = fields
