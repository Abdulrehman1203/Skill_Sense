"""
Clerk Webhook View — handles user provisioning events (user.created, user.updated, user.deleted).

Verifies the Svix HMAC signature on incoming payloads before processing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from svix.webhooks import Webhook, WebhookVerificationError

from .models import CandidateProfile, ClerkWebhookState, RecruiterProfile, User

logger = logging.getLogger(__name__)


class ClerkWebhookView(APIView):
    """
    Webhook endpoint for Clerk user events.
    Secured via Svix HMAC signature verification.
    """

    permission_classes = [AllowAny]
    authentication_classes = []  # Svix authenticates raw bytes, not bearer middleware.

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
            logger.warning("Clerk webhook signature rejected.")
            return Response(
                {"detail": "Invalid webhook signature."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except Exception as exc:
            logger.error("Clerk webhook verification rejected.")
            return Response(
                {"detail": "Webhook verification error."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not isinstance(payload, dict) or not isinstance(payload.get('data', {}), dict):
            return Response({'detail': 'Invalid webhook payload.'}, status=400)
        data = payload.get('data', {})
        if (not isinstance(data.get('id', ''), str) or
            any(not isinstance(data.get(name), (str, type(None))) for name in ('first_name','last_name','image_url')) or
            any(not isinstance(data.get(name, {}), (dict, type(None))) for name in ('public_metadata','unsafe_metadata')) or
            not isinstance(data.get('email_addresses', []), list) or
            any(not isinstance(item, dict) or not isinstance(item.get('email_address', ''), str) for item in data.get('email_addresses', []))):
            return Response({'detail': 'Invalid webhook payload.'}, status=400)
        event_type = payload.get("type")
        data = payload.get("data", {})
        logger.info("Verified Clerk webhook received.")

        if event_type in ("user.created", "user.updated", "user.deleted"):
            # Clerk uses milliseconds for event.created_at; the signed Svix
            # timestamp is a fallback for older/test payloads without it.
            raw_created_at = payload.get("created_at")
            try:
                event_seconds = float(raw_created_at) / 1000 if raw_created_at is not None else float(headers["svix-timestamp"])
                event_at = datetime.fromtimestamp(event_seconds, tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                return Response({"detail": "Invalid event timestamp."}, status=status.HTTP_400_BAD_REQUEST)
            try:
                self._process_user_event(event_type, data, event_at)
            except IntegrityError:
                return Response(
                    {"detail": "Account conflicts with an existing registration."},
                    status=status.HTTP_409_CONFLICT,
                )
        else:
            logger.info("Unhandled Clerk webhook event.")

        return Response({"status": "success"}, status=status.HTTP_200_OK)

    @transaction.atomic
    def _process_user_event(self, event_type: str, data: dict[str, Any], event_at: datetime) -> None:
        clerk_id = data.get("id")
        if not clerk_id:
            logger.error("%s payload missing 'id'.", event_type)
            return

        state, created = ClerkWebhookState.objects.select_for_update().get_or_create(
            clerk_id=clerk_id, defaults={"last_event_at": event_at}
        )
        if not created:
            if event_at < state.last_event_at:
                logger.info('Clerk identity event handled.')
                return
            if state.is_deleted and event_type != "user.deleted":
                logger.info('Clerk identity event handled.')
                return
            if event_at == state.last_event_at and event_type != "user.deleted":
                return

        if event_type == "user.created":
            self._handle_user_created(data)
        elif event_type == "user.updated":
            self._handle_user_updated(data)
        else:
            self._handle_user_deleted(data)
            state.is_deleted = True

        state.last_event_at = event_at
        state.save(update_fields=["last_event_at", "is_deleted"])

    @transaction.atomic
    def _handle_user_created(self, data: dict[str, Any]) -> None:
        clerk_id = data.get("id")
        if not clerk_id:
            logger.error("user.created payload missing 'id'.")
            return

        email = self._extract_primary_email(data)
        if not email:
            raise ValidationError({"email": "A primary email is required."})

        first_name = data.get("first_name") or ""
        last_name = data.get("last_name") or ""
        image_url = (data.get("image_url") or "") if data.get("has_image") is True else ""

        unsafe_metadata = data.get("unsafe_metadata") or {}
        public_metadata = data.get("public_metadata") or {}
        company_name = unsafe_metadata.get("company_name") or public_metadata.get("company_name") or ""

        role = self._extract_role(data, default_role=User.Role.CANDIDATE)

        existing_user = User.objects.filter(email__iexact=email).first()
        if existing_user:
            if existing_user.clerk_id != clerk_id:
                raise ValidationError({'email': 'This email is already registered. Use your existing account login.'})
            else:
                # Same clerk_id, just an update or replay
                # Provisioning is not a local administrator's reactivation
                # decision. Replayed create events must preserve deactivation.
                existing_user.first_name = first_name
                existing_user.last_name = last_name
                existing_user.profile_image_url = image_url
                existing_user.save(update_fields=["first_name", "last_name", "profile_image_url"])
                user = existing_user
                created = False
        else:
            user = User.objects.create(
                clerk_id=clerk_id,
                email=email,
                first_name=first_name,
                last_name=last_name,
                profile_image_url=image_url,
                role=role,
                is_active=True,
            )
            created = True

        # Ensure associated profile exists
        if user.role == User.Role.RECRUITER:
            profile, _ = RecruiterProfile.objects.get_or_create(user=user)
            if company_name and not profile.company_name:
                profile.company_name = company_name
                profile.save()
        else:
            CandidateProfile.objects.get_or_create(user=user)

        logger.info('Clerk provisioning event applied.')

    @transaction.atomic
    def _handle_user_updated(self, data: dict[str, Any]) -> None:
        clerk_id = data.get("id")
        if not clerk_id:
            logger.error("user.updated payload missing 'id'.")
            return

        try:
            user = User.objects.get(clerk_id=clerk_id)
        except User.DoesNotExist:
            logger.info('Clerk identity event handled.')
            self._handle_user_created(data)
            return

        email = self._extract_primary_email(data)
        self._validate_email(email, clerk_id)
        user.email = email
        user.first_name = data.get("first_name") or ""
        user.last_name = data.get("last_name") or ""
        user.profile_image_url = (data.get("image_url") or "") if data.get("has_image") is True else ""
        # Never authorize a role change from user-editable Clerk metadata.
        user.save(update_fields=["email", "first_name", "last_name", "profile_image_url"])

        unsafe_metadata = data.get("unsafe_metadata") or {}
        public_metadata = data.get("public_metadata") or {}
        company_name = unsafe_metadata.get("company_name") or public_metadata.get("company_name") or ""

        # Ensure profile for role exists
        if user.role == User.Role.RECRUITER:
            profile, _ = RecruiterProfile.objects.get_or_create(user=user)
            if company_name and not profile.company_name:
                profile.company_name = company_name
                profile.save()
        elif user.role == User.Role.CANDIDATE:
            CandidateProfile.objects.get_or_create(user=user)

        logger.info('Clerk identity event handled.')

    @transaction.atomic
    def _handle_user_deleted(self, data: dict[str, Any]) -> None:
        clerk_id = data.get("id")
        if not clerk_id:
            logger.error("user.deleted payload missing 'id'.")
            return

        try:
            user = User.objects.get(clerk_id=clerk_id)
            user.is_active = False
            user.save(update_fields=["is_active"])
            logger.info('Clerk identity event handled.')
        except User.DoesNotExist:
            logger.info('Clerk identity event handled.')

    def _validate_email(self, email: str, clerk_id: str) -> None:
        if not email:
            raise ValidationError({"email": "A primary email is required."})
        if User.objects.filter(email__iexact=email).exclude(clerk_id=clerk_id).exists():
            raise ValidationError({"email": "This email is already registered. Use your existing account login."})

    def _extract_primary_email(self, data: dict[str, Any]) -> str:
        primary_email_id = data.get("primary_email_address_id")
        email_addresses = data.get("email_addresses", [])

        for item in email_addresses:
            if item.get("id") == primary_email_id:
                return item.get("email_address", "").strip().lower()

        if email_addresses:
            return email_addresses[0].get("email_address", "").strip().lower()

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
        role_raw = public_metadata.get("role") or unsafe_metadata.get("role")

        if role_raw:
            role_str = str(role_raw).upper()
            if role_str in (User.Role.RECRUITER, User.Role.CANDIDATE):
                return role_str

        return default_role
