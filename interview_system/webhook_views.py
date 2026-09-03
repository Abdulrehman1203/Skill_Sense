"""
Clerk Webhook View — handles user provisioning events (user.created, user.updated, user.deleted).

Verifies the Svix HMAC signature on incoming payloads before processing.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db import transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from svix.webhooks import Webhook, WebhookVerificationError

from .models import CandidateProfile, RecruiterProfile, User

logger = logging.getLogger(__name__)


class ClerkWebhookView(APIView):
    """
    Webhook endpoint for Clerk user events.
    Secured via Svix HMAC signature verification.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Webhooks — Clerk"],
        summary="Clerk Webhook Endpoint",
        description=(
            "Receives user lifecycle webhooks from Clerk (`user.created`, `user.updated`, "
            "`user.deleted`) and provisions/syncs the local User and Profile models.\n\n"
            "Requires valid `svix-id`, `svix-timestamp`, and `svix-signature` headers."
        ),
        request=None,
        responses={
            200: OpenApiResponse(description="Webhook payload verified and processed."),
            400: OpenApiResponse(description="Invalid event type or unhandled format."),
            401: OpenApiResponse(description="Webhook signature verification failed."),
        },
    )
    def post(self, request: Request) -> Response:
        webhook_secret = getattr(settings, "CLERK_WEBHOOK_SECRET", "")
        if not webhook_secret:
            logger.error("CLERK_WEBHOOK_SECRET setting is missing or unconfigured.")
            return Response(
                {"detail": "Webhook secret not configured on server."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        headers = {
            "svix-id": request.headers.get("svix-id", ""),
            "svix-timestamp": request.headers.get("svix-timestamp", ""),
            "svix-signature": request.headers.get("svix-signature", ""),
        }

        body_bytes = request.body

        try:
            wh = Webhook(webhook_secret)
            payload = wh.verify(body_bytes.decode("utf-8"), headers)
        except WebhookVerificationError as exc:
            logger.warning("Clerk Webhook HMAC signature verification failed: %s", exc)
            return Response(
                {"detail": "Invalid webhook signature."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except Exception as exc:
            logger.error("Unexpected error during webhook verification: %s", exc)
            return Response(
                {"detail": "Webhook verification error."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        event_type = payload.get("type")
        data = payload.get("data", {})
        logger.info("Received valid Clerk Webhook event: %s", event_type)

        if event_type == "user.created":
            self._handle_user_created(data)
        elif event_type == "user.updated":
            self._handle_user_updated(data)
        elif event_type == "user.deleted":
            self._handle_user_deleted(data)
        else:
            logger.info("Unhandled Clerk webhook event type: %s", event_type)

        return Response({"status": "success"}, status=status.HTTP_200_OK)

    @transaction.atomic
    def _handle_user_created(self, data: dict[str, Any]) -> None:
        clerk_id = data.get("id")
        if not clerk_id:
            logger.error("user.created payload missing 'id'.")
            return

        email = self._extract_primary_email(data)
        first_name = data.get("first_name") or ""
        last_name = data.get("last_name") or ""

        role = self._extract_role(data, default_role=User.Role.CANDIDATE)

        user, created = User.objects.get_or_create(
            clerk_id=clerk_id,
            defaults={
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
                "role": role,
                "is_active": True,
            },
        )

        if not created:
            # If user already existed, update fields
            user.email = email
            user.first_name = first_name
            user.last_name = last_name
            user.role = role
            user.is_active = True
            user.save()

        # Ensure associated profile exists
        if user.role == User.Role.RECRUITER:
            RecruiterProfile.objects.get_or_create(user=user)
        else:
            CandidateProfile.objects.get_or_create(user=user)

        logger.info(
            "Clerk Webhook: %s user record for clerk_id=%s (email=%s, role=%s).",
            "Created" if created else "Updated existing",
            clerk_id,
            email,
            role,
        )

    @transaction.atomic
    def _handle_user_updated(self, data: dict[str, Any]) -> None:
        clerk_id = data.get("id")
        if not clerk_id:
            logger.error("user.updated payload missing 'id'.")
            return

        try:
            user = User.objects.get(clerk_id=clerk_id)
        except User.DoesNotExist:
            logger.warning("user.updated received for non-existent clerk_id=%s. Provisioning...", clerk_id)
            self._handle_user_created(data)
            return

        user.email = self._extract_primary_email(data)
        user.first_name = data.get("first_name") or ""
        user.last_name = data.get("last_name") or ""
        user.role = self._extract_role(data, default_role=user.role)
        user.save()

        # Ensure profile for role exists
        if user.role == User.Role.RECRUITER:
            RecruiterProfile.objects.get_or_create(user=user)
        elif user.role == User.Role.CANDIDATE:
            CandidateProfile.objects.get_or_create(user=user)

        logger.info("Clerk Webhook: Updated user record for clerk_id=%s.", clerk_id)

    @transaction.atomic
    def _handle_user_deleted(self, data: dict[str, Any]) -> None:
        clerk_id = data.get("id")
        if not clerk_id:
            logger.error("user.deleted payload missing 'id'.")
            return

        try:
            user = User.objects.get(clerk_id=clerk_id)
            user.is_active = False
            user.save()
            logger.info("Clerk Webhook: Deactivated user record for clerk_id=%s.", clerk_id)
        except User.DoesNotExist:
            logger.info("user.deleted received for non-existent clerk_id=%s.", clerk_id)

    def _extract_primary_email(self, data: dict[str, Any]) -> str:
        primary_email_id = data.get("primary_email_address_id")
        email_addresses = data.get("email_addresses", [])

        for item in email_addresses:
            if item.get("id") == primary_email_id:
                return item.get("email_address", "")

        if email_addresses:
            return email_addresses[0].get("email_address", "")

        return ""

    def _extract_role(self, data: dict[str, Any], default_role: str = User.Role.CANDIDATE) -> str:
        """
        Extract role from unsafe_metadata or public_metadata.

        Enforces a strict security whitelist:
        Only 'RECRUITER' and 'CANDIDATE' are accepted.
        Any attempt to specify 'ADMIN' or malformed values via client payloads
        is rejected and defaults to default_role (CANDIDATE).

        ADMIN accounts must strictly be created via Django's own admin/superuser CLI,
        never through Clerk user provisioning webhooks.
        """
        unsafe_metadata = data.get("unsafe_metadata", {}) or {}
        public_metadata = data.get("public_metadata", {}) or {}
        role_raw = unsafe_metadata.get("role") or public_metadata.get("role")

        if role_raw:
            role_str = str(role_raw).upper()
            if role_str in (User.Role.RECRUITER, User.Role.CANDIDATE):
                return role_str

        return default_role
