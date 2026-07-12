"""
Authentication serializers — Login credential validation.

The LoginSerializer validates email + password and returns the authenticated
User instance via `validated_data["user"]`.  Registration serializers remain
in `interview_system.serializers`.
"""

from __future__ import annotations

from django.contrib.auth import authenticate
from rest_framework import serializers

from .models import User


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
#  Response serializer (read-only, for consistent API output)
# ══════════════════════════════════════════════════════════════

class UserResponseSerializer(serializers.ModelSerializer):
    """Minimal user payload returned after login / token verification."""

    class Meta:  # pyrefly: ignore[bad-override]
        model = User
        fields = ["id", "email", "first_name", "last_name", "role", "is_verified", "created_at", "is_active"]
        read_only_fields = fields
