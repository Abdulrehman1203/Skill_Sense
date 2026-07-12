"""
Authentication views — Register for Recruiter and Candidate.

Endpoints:
    POST /api/auth/register/recruiter/   – create a RECRUITER account
    POST /api/auth/register/candidate/   – create a CANDIDATE account

Note: Login, Refresh, Logout, and MeView are in the `authentication` app.
"""

from __future__ import annotations

from django.db import transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import (
    CandidateRegisterSerializer,
    RecruiterRegisterSerializer,
    UserResponseSerializer,
)



# ══════════════════════════════════════════════════════════════
#  Register — Recruiter
# ══════════════════════════════════════════════════════════════

class RecruiterRegisterView(APIView):
    """Register a new RECRUITER user + profile in one atomic request."""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Auth — Register"],
        summary="Register a Recruiter",
        description=(
            "Creates a new user with **role=RECRUITER** and an associated `RecruiterProfile`.\n\n"
            "- All profile fields (`company_name`, `industry`, etc.) are **optional** at signup."
        ),
        request=RecruiterRegisterSerializer,
        responses={
            201: OpenApiResponse(
                response=UserResponseSerializer,
                description="Recruiter created — includes user info.",
            ),
            400: OpenApiResponse(description="Validation error (e.g. email taken, passwords don't match)."),
        },
    )
    @transaction.atomic
    def post(self, request: Request) -> Response:
        serializer = RecruiterRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        return Response(
            {
                "message": "Recruiter account created successfully.",
                "user": UserResponseSerializer(user).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ══════════════════════════════════════════════════════════════
#  Register — Candidate
# ══════════════════════════════════════════════════════════════

class CandidateRegisterView(APIView):
    """Register a new CANDIDATE user + profile in one atomic request."""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Auth — Register"],
        summary="Register a Candidate",
        description=(
            "Creates a new user with **role=CANDIDATE** and an associated `CandidateProfile`.\n\n"
            "- All profile fields (`phone`, `headline`, `linkedin_url`, etc.) are **optional** at signup."
        ),
        request=CandidateRegisterSerializer,
        responses={
            201: OpenApiResponse(
                response=UserResponseSerializer,
                description="Candidate created — includes user info.",
            ),
            400: OpenApiResponse(description="Validation error (e.g. email taken, passwords don't match)."),
        },
    )
    @transaction.atomic
    def post(self, request: Request) -> Response:
        serializer = CandidateRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        return Response(
            {
                "message": "Candidate account created successfully.",
                "user": UserResponseSerializer(user).data,
            },
            status=status.HTTP_201_CREATED,
        )

