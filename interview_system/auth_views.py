"""
JWT Authentication views — Login, Refresh, Logout, and MeView.

Endpoints:
    POST /api/auth/login/     – authenticate and issue JWT tokens
    POST /api/auth/refresh/   – rotate refresh token (cookie-based)
    POST /api/auth/logout/    – blacklist refresh token and clear cookie
    GET  /api/users/me/       – return the current authenticated user's info
"""

from __future__ import annotations
from typing import Any, cast

from django.conf import settings
from django.contrib.auth import get_user_model
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .auth_serializers import LoginSerializer, UserResponseSerializer

User = get_user_model()


# ── Helper ────────────────────────────────────────────────────

def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    """Attach the refresh token as an httpOnly, Secure cookie."""
    response.set_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=settings.REFRESH_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.REFRESH_COOKIE_SECURE,
        samesite=settings.REFRESH_COOKIE_SAMESITE,
        path="/api/auth/refresh/",  # cookie only sent to the refresh endpoint
    )


def _delete_refresh_cookie(response: Response) -> None:
    """Remove the refresh cookie from the client."""
    response.delete_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        path="/api/auth/refresh/",
    )


# ══════════════════════════════════════════════════════════════
#  Login
# ══════════════════════════════════════════════════════════════

class LoginView(APIView):
    """
    Authenticate with email + password.

    On success:
    - Access token is returned in the JSON response body.
    - Refresh token is set as an httpOnly, Secure cookie scoped
      to ``/api/auth/refresh/`` only.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Auth — Login"],
        summary="Login (all roles)",
        description=(
            "Authenticate a **Recruiter**, **Candidate**, or **Admin** using "
            "email and password.\n\n"
            "Returns a JWT **access token** in the response body and sets the "
            "**refresh token** as an `httpOnly` cookie."
        ),
        request=LoginSerializer,
        responses={
            200: OpenApiResponse(description="Login successful — access token + user info."),
            400: OpenApiResponse(description="Invalid credentials or inactive account."),
        },
    )
    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]

        # Issue JWT tokens
        refresh = RefreshToken.for_user(user)
        access = str(refresh.access_token)

        response = Response(
            {
                "message": "Login successful.",
                "access": access,
                "user": UserResponseSerializer(user).data,
            },
            status=status.HTTP_200_OK,
        )

        # Set refresh token in httpOnly cookie
        _set_refresh_cookie(response, str(refresh))
        return response


# ══════════════════════════════════════════════════════════════
#  Refresh (custom, cookie-aware)
# ══════════════════════════════════════════════════════════════

class RefreshView(APIView):
    """
    Exchange a valid refresh token (read from the httpOnly cookie)
    for a new access token.

    When ``ROTATE_REFRESH_TOKENS`` is enabled, a brand-new refresh
    token is also issued and the old one is blacklisted.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Auth — Token"],
        summary="Refresh access token",
        description=(
            "Reads the refresh token from the `httpOnly` cookie, validates it, "
            "and returns a new access token.\n\n"
            "If token rotation is enabled, a new refresh cookie is also set "
            "and the old refresh token is blacklisted."
        ),
        request=None,
        responses={
            200: OpenApiResponse(description="New access token issued."),
            401: OpenApiResponse(description="Refresh token missing, invalid, or expired."),
        },
    )
    def post(self, request: Request) -> Response:
        raw_refresh = request.COOKIES.get(settings.REFRESH_COOKIE_NAME)
        if not raw_refresh:
            return Response(
                {"detail": "No refresh token found."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            old_refresh = RefreshToken(cast(Any, raw_refresh))

            # Extract the new access token from the OLD refresh token
            new_access = str(old_refresh.access_token)

            # Blacklist the old refresh token (if BLACKLIST_AFTER_ROTATION is True)
            old_refresh.blacklist()

        except TokenError:
            response = Response(
                {"detail": "Refresh token invalid or expired."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
            _delete_refresh_cookie(response)
            return response

        # Issue a brand-new refresh token for the same user
        user_id = old_refresh["user_id"]
        user = User.objects.get(pk=user_id)
        new_refresh = RefreshToken.for_user(user)

        response = Response(
            {"access": new_access},
            status=status.HTTP_200_OK,
        )
        _set_refresh_cookie(response, str(new_refresh))
        return response


# ══════════════════════════════════════════════════════════════
#  Logout
# ══════════════════════════════════════════════════════════════

class LogoutView(APIView):
    """
    Blacklist the current refresh token and clear the cookie.

    Even if someone had intercepted the refresh token earlier,
    it can no longer be exchanged for a new access token.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Auth — Login"],
        summary="Logout",
        description=(
            "Blacklists the current refresh token (read from the cookie) "
            "so it can never be reused, and deletes the cookie."
        ),
        request=None,
        responses={
            205: OpenApiResponse(description="Logged out — refresh token blacklisted."),
        },
    )
    def post(self, request: Request) -> Response:
        raw_refresh = request.COOKIES.get(settings.REFRESH_COOKIE_NAME)
        if raw_refresh:
            try:
                RefreshToken(cast(Any, raw_refresh)).blacklist()
            except TokenError:
                pass  # token already expired / blacklisted — nothing to do

        response = Response(status=status.HTTP_205_RESET_CONTENT)
        _delete_refresh_cookie(response)
        return response


# ══════════════════════════════════════════════════════════════
#  MeView — Phase 3.5 "prove the chain" endpoint
# ══════════════════════════════════════════════════════════════

class MeView(APIView):
    """
    Return the current authenticated user's info.

    Requires a valid ``Authorization: Bearer <access_token>`` header.
    This endpoint proves the full signup → login → token → verification chain.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Auth — User"],
        summary="Get current user",
        description=(
            "Returns the authenticated user's profile information.\n\n"
            "Requires a valid `Authorization: Bearer <access_token>` header."
        ),
        responses={
            200: OpenApiResponse(
                response=UserResponseSerializer,
                description="Current user info.",
            ),
            401: OpenApiResponse(description="Invalid or missing access token."),
        },
    )
    def get(self, request: Request) -> Response:
        return Response(
            UserResponseSerializer(request.user).data,
            status=status.HTTP_200_OK,
        )
