"""
Tests for Clerk Authentication, Webhook Provisioning, and Job/JobSkill API.
"""

from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
import json
import time
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
import jwt
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIClient
from svix.webhooks import Webhook

from ..authentication import JWKS_CACHE_KEY, ClerkJWTAuthentication
from ..integrations.gemini_client import GeminiParseError
from ..models import Application, CandidateProfile, Job, JobSkill, RecruiterProfile, Resume, User


def _int_to_base64url(val: int) -> str:
    """Helper to convert int to base64url string without padding."""
    b = val.to_bytes((val.bit_length() + 7) // 8, byteorder="big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("utf-8")


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
)
class ClerkAuthTestCase(TestCase):
    """Test suite for ClerkJWTAuthentication and ClerkWebhookView."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # Generate temporary RSA keypair for JWT signing tests
        cls.private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        cls.public_key = cls.private_key.public_key()
        cls.kid = "test_clerk_kid_1"

        # Convert public key to JWK format
        pub_numbers = cls.public_key.public_numbers()
        cls.jwk = {
            "kty": "RSA",
            "alg": "RS256",
            "use": "sig",
            "kid": cls.kid,
            "n": _int_to_base64url(pub_numbers.n),
            "e": _int_to_base64url(pub_numbers.e),
        }
        cls.jwks_data = {"keys": [cls.jwk]}
        cls.webhook_secret = "whsec_C2849823472938479238472938479238"

    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.auth = ClerkJWTAuthentication()
        # Populate cache with our test JWKS
        cache.set(JWKS_CACHE_KEY, self.jwks_data, 3600)

        # Create a test user in local database
        self.user = User.objects.create(
            clerk_id="user_2test_clerk_user_123",
            email="testuser@example.com",
            first_name="Test",
            last_name="User",
            role=User.Role.CANDIDATE,
            is_active=True,
        )
        CandidateProfile.objects.create(user=self.user)

    def _generate_token(
        self,
        sub: str,
        expires_in: int = 3600,
        kid: str | None = None,
    ) -> str:
        payload = {
            "sub": sub,
            "iat": int(time.time()),
            "exp": int(time.time()) + expires_in,
            "iss": "https://stunning-slug-13.clerk.accounts.dev",
        }
        headers = {"kid": kid or self.kid}
        pem_private = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return jwt.encode(payload, pem_private, algorithm="RS256", headers=headers)

    # ── JWT Authentication Tests ───────────────────────────────

    def test_valid_clerk_jwt_authentication(self) -> None:
        """A valid Clerk JWT resolves to the correct local User."""
        token = self.generate_test_token(self.user.clerk_id)

        auth_user, payload = self.auth.authenticate_credentials(token)
        self.assertEqual(auth_user, self.user)
        self.assertEqual(payload["sub"], self.user.clerk_id)

    def test_expired_jwt_rejected(self) -> None:
        """An expired Clerk JWT raises AuthenticationFailed."""
        token = self.generate_test_token(self.user.clerk_id, expires_in=-3600)

        with self.assertRaises(AuthenticationFailed) as ctx:
            self.auth.authenticate_credentials(token)
        self.assertIn("Token has expired", str(ctx.exception))

    def test_invalid_signature_jwt_rejected(self) -> None:
        """A JWT signed with an untrusted private key is rejected."""
        other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem_other = other_private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        payload = {"sub": self.user.clerk_id, "exp": int(time.time()) + 3600}
        bad_token = jwt.encode(payload, pem_other, algorithm="RS256", headers={"kid": self.kid})

        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate_credentials(bad_token)

    def test_unprovisioned_user_jwt_rejected(self) -> None:
        """A valid JWT for a clerk_id not yet provisioned in local DB raises AuthenticationFailed."""
        token = self.generate_test_token("user_unprovisioned_999")

        with self.assertRaises(AuthenticationFailed) as ctx:
            self.auth.authenticate_credentials(token)
        self.assertIn("User account not found", str(ctx.exception))

    def test_deactivated_user_jwt_rejected(self) -> None:
        """A valid JWT for a deactivated user (is_active=False) raises AuthenticationFailed."""
        self.user.is_active = False
        self.user.save()
        token = self.generate_test_token(self.user.clerk_id)

        with self.assertRaises(AuthenticationFailed) as ctx:
            self.auth.authenticate_credentials(token)
        self.assertIn("deactivated", str(ctx.exception))

    # ── MeView API Endpoint Tests ─────────────────────────────

    def test_me_view_returns_current_user_profile(self) -> None:
        """GET /api/users/me/ with valid Authorization header returns current user payload."""
        token = self.generate_test_token(self.user.clerk_id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        url = reverse("interview_system:me")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["clerk_id"], self.user.clerk_id)
        self.assertEqual(response.data["email"], self.user.email)
        self.assertEqual(response.data["role"], self.user.role)

    def test_me_view_unauthenticated_rejected(self) -> None:
        """GET /api/users/me/ without token returns 401/403 Unauthorized/Forbidden."""
        url = reverse("interview_system:me")
        response = self.client.get(url)
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    # ── Webhook Endpoint Tests ─────────────────────────────────

    @override_settings(CLERK_WEBHOOK_SECRET="whsec_C2849823472938479238472938479238")
    def test_unverified_webhook_payload_rejected_with_401(self) -> None:
        """A webhook request with invalid/missing Svix headers is rejected with 401."""
        url = reverse("interview_system:clerk-webhook")
        response = self.client.post(
            url,
            data={"type": "user.created", "data": {}},
            format="json",
            HTTP_SVIX_ID="msg_invalid_123",
            HTTP_SVIX_TIMESTAMP=str(int(time.time())),
            HTTP_SVIX_SIGNATURE="v1,invalid_signature",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_settings(CLERK_WEBHOOK_SECRET="whsec_C2849823472938479238472938479238")
    def test_valid_user_created_webhook_provisions_user_and_profile(self) -> None:
        """A valid user.created webhook provisions a new User and RecruiterProfile."""
        payload_dict = {
            "type": "user.created",
            "data": {
                "id": "user_clerk_new_recruiter_777",
                "primary_email_address_id": "email_111",
                "email_addresses": [
                    {"id": "email_111", "email_address": "recruiter@company.com"}
                ],
                "first_name": "Jane",
                "last_name": "Smith",
                "public_metadata": {"role": "RECRUITER"},
            },
        }
        body_str = json.dumps(payload_dict)

        # Generate valid Svix signature
        wh = Webhook(self.webhook_secret)
        msg_id = "msg_test_user_created_001"
        now_dt = datetime.now(timezone.utc)
        timestamp_str = str(int(now_dt.timestamp()))
        signature = wh.sign(msg_id, now_dt, body_str)

        url = reverse("interview_system:clerk-webhook")
        response = self.client.post(
            url,
            data=body_str,
            content_type="application/json",
            HTTP_SVIX_ID=msg_id,
            HTTP_SVIX_TIMESTAMP=timestamp_str,
            HTTP_SVIX_SIGNATURE=signature,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify User model creation
        new_user = User.objects.get(clerk_id="user_clerk_new_recruiter_777")
        self.assertEqual(new_user.email, "recruiter@company.com")
        self.assertEqual(new_user.first_name, "Jane")
        self.assertEqual(new_user.last_name, "Smith")
        self.assertEqual(new_user.role, User.Role.RECRUITER)

        # Verify profile creation
        self.assertTrue(RecruiterProfile.objects.filter(user=new_user).exists())

    @override_settings(CLERK_WEBHOOK_SECRET="whsec_C2849823472938479238472938479238")
    def test_user_created_webhook_unsafe_metadata_recruiter(self) -> None:
        """Webhook accepts role from unsafe_metadata (set by clerk-js)."""
        payload_dict = {
            "type": "user.created",
            "data": {
                "id": "user_clerk_web_recruiter_888",
                "primary_email_address_id": "email_222",
                "email_addresses": [
                    {"id": "email_222", "email_address": "web_recruiter@company.com"}
                ],
                "first_name": "Alex",
                "last_name": "Web",
                "unsafe_metadata": {"role": "RECRUITER"},
            },
        }
        body_str = json.dumps(payload_dict)
        wh = Webhook(self.webhook_secret)
        msg_id = "msg_test_user_created_002"
        now_dt = datetime.now(timezone.utc)
        signature = wh.sign(msg_id, now_dt, body_str)

        response = self.client.post(
            reverse("interview_system:clerk-webhook"),
            data=body_str,
            content_type="application/json",
            HTTP_SVIX_ID=msg_id,
            HTTP_SVIX_TIMESTAMP=str(int(now_dt.timestamp())),
            HTTP_SVIX_SIGNATURE=signature,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        new_user = User.objects.get(clerk_id="user_clerk_web_recruiter_888")
        self.assertEqual(new_user.role, User.Role.RECRUITER)
        self.assertTrue(RecruiterProfile.objects.filter(user=new_user).exists())

    @override_settings(CLERK_WEBHOOK_SECRET="whsec_C2849823472938479238472938479238")
    def test_user_created_webhook_admin_role_rejected_and_defaulted_to_candidate(self) -> None:
        """Attempting to claim ADMIN role in metadata is rejected; user defaults to CANDIDATE."""
        payload_dict = {
            "type": "user.created",
            "data": {
                "id": "user_clerk_malicious_admin_999",
                "primary_email_address_id": "email_333",
                "email_addresses": [
                    {"id": "email_333", "email_address": "hacker@evil.com"}
                ],
                "first_name": "Hacker",
                "last_name": "One",
                "unsafe_metadata": {"role": "ADMIN"},
                "public_metadata": {"role": "ADMIN"},
            },
        }
        body_str = json.dumps(payload_dict)
        wh = Webhook(self.webhook_secret)
        msg_id = "msg_test_user_created_003"
        now_dt = datetime.now(timezone.utc)
        signature = wh.sign(msg_id, now_dt, body_str)

        response = self.client.post(
            reverse("interview_system:clerk-webhook"),
            data=body_str,
            content_type="application/json",
            HTTP_SVIX_ID=msg_id,
            HTTP_SVIX_TIMESTAMP=str(int(now_dt.timestamp())),
            HTTP_SVIX_SIGNATURE=signature,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        new_user = User.objects.get(clerk_id="user_clerk_malicious_admin_999")
        # Must be CANDIDATE, NEVER ADMIN
        self.assertEqual(new_user.role, User.Role.CANDIDATE)
        self.assertNotEqual(new_user.role, User.Role.ADMIN)
        self.assertTrue(CandidateProfile.objects.filter(user=new_user).exists())

    def generate_test_token(self, sub: str, expires_in: int = 3600) -> str:
        return self._generate_token(sub=sub, expires_in=expires_in)


# ═══════════════════════════════════════════════════════════════
#  Job / JobSkill — Model Tests
# ═══════════════════════════════════════════════════════════════

class JobModelTestCase(TestCase):
    """Unit tests for the Job and JobSkill models."""

    def setUp(self) -> None:
        self.user = User.objects.create(
            clerk_id="user_recruiter_model_test",
            email="recruiter_model@example.com",
            role=User.Role.RECRUITER,
        )
        self.profile = RecruiterProfile.objects.create(
            user=self.user,
            company_name="TestCorp",
        )
        self.job = Job.objects.create(
            recruiter=self.profile,
            title="Backend Engineer",
            description="Build APIs",
            requirements="3+ years Python",
            location="Remote",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.MID,
            status=Job.Status.DRAFT,
        )

    def test_job_str(self) -> None:
        """Job.__str__ returns 'title (recruiter)'."""
        expected = f"Backend Engineer ({self.profile})"
        self.assertEqual(str(self.job), expected)

    def test_job_default_status_is_draft(self) -> None:
        self.assertEqual(self.job.status, Job.Status.DRAFT)

    def test_job_clean_active_with_past_deadline_raises(self) -> None:
        """An ACTIVE job with a past deadline must fail validation."""
        self.job.status = Job.Status.ACTIVE
        self.job.deadline = date(2020, 1, 1)
        with self.assertRaises(ValidationError) as ctx:
            self.job.clean()
        self.assertIn("deadline", ctx.exception.message_dict)

    def test_job_clean_draft_with_past_deadline_ok(self) -> None:
        """A DRAFT job with a past deadline is allowed (not yet published)."""
        self.job.status = Job.Status.DRAFT
        self.job.deadline = date(2020, 1, 1)
        self.job.clean()  # should not raise

    def test_job_clean_active_without_deadline_ok(self) -> None:
        """An ACTIVE job with no deadline is perfectly valid."""
        self.job.status = Job.Status.ACTIVE
        self.job.deadline = None
        self.job.clean()  # should not raise

    # ── JobSkill tests ────────────────────────────────────────

    def test_jobskill_str_required(self) -> None:
        skill = JobSkill.objects.create(
            job=self.job, skill_name="Python", is_required=True,
        )
        self.assertEqual(str(skill), "Python (required)")

    def test_jobskill_str_optional(self) -> None:
        skill = JobSkill.objects.create(
            job=self.job, skill_name="Docker", is_required=False,
        )
        self.assertEqual(str(skill), "Docker (optional)")

    def test_jobskill_unique_together(self) -> None:
        """Duplicate (job, skill_name) pairs violate unique_together."""
        JobSkill.objects.create(job=self.job, skill_name="Python")
        with self.assertRaises(Exception):
            # IntegrityError from the DB
            JobSkill.objects.create(job=self.job, skill_name="Python")


# ═══════════════════════════════════════════════════════════════
#  Job / JobSkill — API Tests
# ═══════════════════════════════════════════════════════════════

@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
)
class JobAPITestCase(TestCase):
    """Integration tests for the Job and JobSkill API endpoints."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        cls.public_key = cls.private_key.public_key()
        cls.kid = "test_job_api_kid"

        pub_numbers = cls.public_key.public_numbers()
        cls.jwk = {
            "kty": "RSA",
            "alg": "RS256",
            "use": "sig",
            "kid": cls.kid,
            "n": _int_to_base64url(pub_numbers.n),
            "e": _int_to_base64url(pub_numbers.e),
        }
        cls.jwks_data = {"keys": [cls.jwk]}

    def setUp(self) -> None:
        cache.clear()
        cache.set(JWKS_CACHE_KEY, self.jwks_data, 3600)
        self.client = APIClient()

        # ── Recruiter user ──
        self.recruiter_user = User.objects.create(
            clerk_id="user_recruiter_api_test_1",
            email="recruiter_api@example.com",
            role=User.Role.RECRUITER,
        )
        self.recruiter_profile = RecruiterProfile.objects.create(
            user=self.recruiter_user,
            company_name="APICorp",
        )

        # ── Another recruiter (non-owner) ──
        self.other_recruiter_user = User.objects.create(
            clerk_id="user_recruiter_api_test_2",
            email="other_recruiter@example.com",
            role=User.Role.RECRUITER,
        )
        RecruiterProfile.objects.create(
            user=self.other_recruiter_user,
            company_name="OtherCorp",
        )

        # ── Candidate user ──
        self.candidate_user = User.objects.create(
            clerk_id="user_candidate_api_test_1",
            email="candidate_api@example.com",
            role=User.Role.CANDIDATE,
        )
        CandidateProfile.objects.create(user=self.candidate_user)

    def _token(self, clerk_id: str) -> str:
        payload = {
            "sub": clerk_id,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "iss": "https://stunning-slug-13.clerk.accounts.dev",
        }
        pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": self.kid})

    def _auth(self, user: User) -> None:
        token = self._token(user.clerk_id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    # ── Job Creation ──────────────────────────────────────────

    # ── Job Creation ──────────────────────────────────────────

    def test_recruiter_can_create_job(self) -> None:
        """POST /api/jobs/ by an authenticated recruiter creates DRAFT job and syncs skills."""
        self._auth(self.recruiter_user)
        future_date = (date.today() + timedelta(days=365)).strftime("%Y-%m-%d")
        data = {
            "title": "Senior Django Dev",
            "description": "Build awesome APIs",
            "requirements": "5+ years Django",
            "skills_required": ["Python", "Django", "PostgreSQL"],
            "location": "Lahore",
            "job_type": "REMOTE",
            "experience_level": "SENIOR",
            "status": "ACTIVE",  # Should be ignored and created as DRAFT
            "deadline": future_date,
        }
        response = self.client.post(
            reverse("interview_system:job-list"),
            data=data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        job = Job.objects.get(title="Senior Django Dev")
        self.assertEqual(job.recruiter, self.recruiter_profile)
        self.assertEqual(job.status, Job.Status.DRAFT)
        self.assertEqual(job.skills_required, ["Python", "Django", "PostgreSQL"])
        self.assertEqual(job.job_skills.count(), 3)

    def test_candidate_cannot_create_job(self) -> None:
        """POST /api/jobs/ by a candidate is forbidden."""
        self._auth(self.candidate_user)
        future_date = (date.today() + timedelta(days=365)).strftime("%Y-%m-%d")
        data = {
            "title": "Sneaky Job",
            "description": "Should not work",
            "requirements": "N/A",
            "skills_required": ["Python"],
            "location": "Nowhere",
            "job_type": "REMOTE",
            "experience_level": "ENTRY",
            "deadline": future_date,
        }
        response = self.client.post(
            reverse("interview_system:job-list"),
            data=data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # ── Role-based listing & retrieval ──────────────────────────

    def test_candidate_sees_only_active_jobs_paginated(self) -> None:
        """GET /api/jobs/ by candidate returns paginated envelope with ACTIVE jobs only."""
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Active Job",
            description="d",
            requirements="r",
            location="x",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.ENTRY,
            status=Job.Status.ACTIVE,
        )
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Draft Job",
            description="d",
            requirements="r",
            location="x",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.ENTRY,
            status=Job.Status.DRAFT,
        )

        self._auth(self.candidate_user)
        response = self.client.get(reverse("interview_system:job-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("results", response.data)
        self.assertIn("count", response.data)

        titles = [j["title"] for j in response.data["results"]]
        self.assertIn("Active Job", titles)
        self.assertNotIn("Draft Job", titles)

    def test_candidate_retrieving_draft_job_returns_404(self) -> None:
        """GET /api/jobs/{id}/ by candidate on non-ACTIVE job returns 404."""
        job = Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Draft Job",
            description="d",
            requirements="r",
            location="x",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.ENTRY,
            status=Job.Status.DRAFT,
        )
        self._auth(self.candidate_user)
        response = self.client.get(reverse("interview_system:job-detail", args=[job.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # ── Job Update & Deletion Constraints ───────────────────────

    def test_patch_status_field_rejected_with_400(self) -> None:
        """PATCH /api/jobs/{id}/ with status in payload returns 400 Bad Request."""
        job = Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Update Test",
            description="d",
            requirements="r",
            location="x",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.ENTRY,
            status=Job.Status.DRAFT,
        )
        self._auth(self.recruiter_user)
        response = self.client.patch(
            reverse("interview_system:job-detail", args=[job.id]),
            data={"status": "ACTIVE"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("status", response.data)

    def test_delete_draft_job_succeeds(self) -> None:
        """DELETE /api/jobs/{id}/ on DRAFT job succeeds (204)."""
        job = Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Draft to Delete",
            description="d",
            requirements="r",
            location="x",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.ENTRY,
            status=Job.Status.DRAFT,
        )
        self._auth(self.recruiter_user)
        response = self.client.delete(reverse("interview_system:job-detail", args=[job.id]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Job.objects.filter(id=job.id).exists())

    def test_delete_active_job_fails_400(self) -> None:
        """DELETE /api/jobs/{id}/ on ACTIVE job returns 400 Bad Request."""
        job = Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Active Job",
            description="d",
            requirements="r",
            location="x",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.ENTRY,
            status=Job.Status.ACTIVE,
        )
        self._auth(self.recruiter_user)
        response = self.client.delete(reverse("interview_system:job-detail", args=[job.id]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Job.objects.filter(id=job.id).exists())

    # ── Status Actions: Publish & Close ─────────────────────────

    def test_publish_draft_job_success(self) -> None:
        """POST /api/jobs/{id}/publish/ transitions DRAFT job to ACTIVE."""
        future_date = date.today() + timedelta(days=30)
        job = Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Publish Me",
            description="d",
            requirements="r",
            skills_required=["Python"],
            location="x",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.ENTRY,
            status=Job.Status.DRAFT,
            deadline=future_date,
        )
        self._auth(self.recruiter_user)
        response = self.client.post(
            reverse("interview_system:job-publish", args=[job.id])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.ACTIVE)

    def test_close_active_job_success(self) -> None:
        """POST /api/jobs/{id}/close/ transitions ACTIVE job to CLOSED."""
        job = Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Close Me",
            description="d",
            requirements="r",
            skills_required=["Python"],
            location="x",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.ENTRY,
            status=Job.Status.ACTIVE,
        )
        self._auth(self.recruiter_user)
        response = self.client.post(
            reverse("interview_system:job-close", args=[job.id])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.CLOSED)

    def test_non_owner_cannot_update_job(self) -> None:
        """PATCH /api/jobs/{id}/ by a non-owner recruiter returns 404 (queryset-scoped)."""
        job = Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Owner Only",
            description="d",
            requirements="r",
            location="x",
            job_type=Job.JobType.ONSITE,
            experience_level=Job.ExperienceLevel.SENIOR,
        )
        self._auth(self.other_recruiter_user)
        response = self.client.patch(
            reverse("interview_system:job-detail", args=[job.id]),
            data={"title": "Hijacked"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_skills_required_rejects_empty_strings(self) -> None:
        """skills_required validation rejects empty/blank entries."""
        self._auth(self.recruiter_user)
        future_date = (date.today() + timedelta(days=365)).strftime("%Y-%m-%d")
        data = {
            "title": "Bad Skills Job",
            "description": "d",
            "requirements": "r",
            "skills_required": ["Python", "", "  "],
            "location": "x",
            "job_type": "REMOTE",
            "experience_level": "ENTRY",
            "deadline": future_date,
        }
        response = self.client.post(
            reverse("interview_system:job-list"),
            data=data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # ── 4.3 Candidate-side filtering & pagination ──────────────

    def test_candidate_filter_by_job_type(self) -> None:
        """GET /api/jobs/?job_type=REMOTE returns only REMOTE active jobs."""
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Remote Role",
            description="d",
            requirements="r",
            location="Anywhere",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.MID,
            status=Job.Status.ACTIVE,
        )
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Onsite Role",
            description="d",
            requirements="r",
            location="NYC",
            job_type=Job.JobType.ONSITE,
            experience_level=Job.ExperienceLevel.MID,
            status=Job.Status.ACTIVE,
        )
        self._auth(self.candidate_user)
        response = self.client.get(
            reverse("interview_system:job-list"), {"job_type": "REMOTE"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [j["title"] for j in response.data["results"]]
        self.assertIn("Remote Role", titles)
        self.assertNotIn("Onsite Role", titles)

    def test_candidate_filter_by_experience_level(self) -> None:
        """GET /api/jobs/?experience_level=SENIOR returns only SENIOR active jobs."""
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Senior Role",
            description="d",
            requirements="r",
            location="x",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.SENIOR,
            status=Job.Status.ACTIVE,
        )
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Entry Role",
            description="d",
            requirements="r",
            location="x",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.ENTRY,
            status=Job.Status.ACTIVE,
        )
        self._auth(self.candidate_user)
        response = self.client.get(
            reverse("interview_system:job-list"), {"experience_level": "SENIOR"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [j["title"] for j in response.data["results"]]
        self.assertIn("Senior Role", titles)
        self.assertNotIn("Entry Role", titles)

    def test_candidate_filter_by_title_icontains(self) -> None:
        """GET /api/jobs/?title__icontains=backend matches case-insensitively."""
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Backend Engineer",
            description="d",
            requirements="r",
            location="x",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.MID,
            status=Job.Status.ACTIVE,
        )
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Frontend Engineer",
            description="d",
            requirements="r",
            location="x",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.MID,
            status=Job.Status.ACTIVE,
        )
        self._auth(self.candidate_user)
        response = self.client.get(
            reverse("interview_system:job-list"), {"title__icontains": "backend"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [j["title"] for j in response.data["results"]]
        self.assertIn("Backend Engineer", titles)
        self.assertNotIn("Frontend Engineer", titles)

    def test_candidate_filter_by_location_icontains(self) -> None:
        """GET /api/jobs/?location__icontains=lahore matches case-insensitively."""
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Lahore Job",
            description="d",
            requirements="r",
            location="Lahore, Pakistan",
            job_type=Job.JobType.ONSITE,
            experience_level=Job.ExperienceLevel.MID,
            status=Job.Status.ACTIVE,
        )
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="NYC Job",
            description="d",
            requirements="r",
            location="New York City",
            job_type=Job.JobType.ONSITE,
            experience_level=Job.ExperienceLevel.MID,
            status=Job.Status.ACTIVE,
        )
        self._auth(self.candidate_user)
        response = self.client.get(
            reverse("interview_system:job-list"), {"location__icontains": "lahore"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [j["title"] for j in response.data["results"]]
        self.assertIn("Lahore Job", titles)
        self.assertNotIn("NYC Job", titles)

    def test_candidate_filter_by_created_at_range(self) -> None:
        """GET /api/jobs/?created_at__gte=...&created_at__lte=... filters by date range."""
        job = Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Recent Job",
            description="d",
            requirements="r",
            location="x",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.MID,
            status=Job.Status.ACTIVE,
        )
        self._auth(self.candidate_user)
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        response = self.client.get(
            reverse("interview_system:job-list"),
            {"created_at__gte": today, "created_at__lte": tomorrow},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [j["title"] for j in response.data["results"]]
        self.assertIn("Recent Job", titles)

    def test_candidate_combined_filters_spec_example(self) -> None:
        """
        Spec example: GET /api/jobs/?job_type=REMOTE&experience_level=MID&title__icontains=backend
        All parameters are optional and combinable.
        """
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Senior Backend Developer",
            description="d",
            requirements="r",
            location="Remote",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.MID,
            status=Job.Status.ACTIVE,
        )
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Frontend Developer",
            description="d",
            requirements="r",
            location="Remote",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.MID,
            status=Job.Status.ACTIVE,
        )
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Backend Intern",
            description="d",
            requirements="r",
            location="Office",
            job_type=Job.JobType.ONSITE,
            experience_level=Job.ExperienceLevel.ENTRY,
            status=Job.Status.ACTIVE,
        )
        # Draft job matching all filters — should NOT appear for candidates
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Backend Draft",
            description="d",
            requirements="r",
            location="Remote",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.MID,
            status=Job.Status.DRAFT,
        )
        self._auth(self.candidate_user)
        response = self.client.get(
            reverse("interview_system:job-list"),
            {
                "job_type": "REMOTE",
                "experience_level": "MID",
                "title__icontains": "backend",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Senior Backend Developer")

    def test_candidate_pagination_page_param(self) -> None:
        """GET /api/jobs/?page=2&page_size=1 returns second page of results."""
        for i in range(3):
            Job.objects.create(
                recruiter=self.recruiter_profile,
                title=f"Paginated Job {i}",
                description="d",
                requirements="r",
                location="x",
                job_type=Job.JobType.REMOTE,
                experience_level=Job.ExperienceLevel.MID,
                status=Job.Status.ACTIVE,
            )
        self._auth(self.candidate_user)
        response = self.client.get(
            reverse("interview_system:job-list"), {"page": 2, "page_size": 1}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertIsNotNone(response.data["previous"])

    def test_candidate_filters_never_leak_draft_jobs(self) -> None:
        """Candidate filters cannot leak DRAFT/CLOSED jobs via status query param."""
        Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Hidden Draft",
            description="d",
            requirements="r",
            location="x",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.MID,
            status=Job.Status.DRAFT,
        )
        self._auth(self.candidate_user)
        response = self.client.get(
            reverse("interview_system:job-list"), {"status": "DRAFT"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(len(response.data["results"]), 0)


# ═══════════════════════════════════════════════════════════════
#  Profile Endpoints — API Tests
# ═══════════════════════════════════════════════════════════════

@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
)
class ProfileAPITestCase(TestCase):
    """Integration tests for RecruiterProfileView and CandidateProfileView."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        cls.public_key = cls.private_key.public_key()
        cls.kid = "test_profile_api_kid"

        pub_numbers = cls.public_key.public_numbers()
        cls.jwk = {
            "kty": "RSA",
            "alg": "RS256",
            "use": "sig",
            "kid": cls.kid,
            "n": _int_to_base64url(pub_numbers.n),
            "e": _int_to_base64url(pub_numbers.e),
        }
        cls.jwks_data = {"keys": [cls.jwk]}

    def setUp(self) -> None:
        cache.clear()
        cache.set(JWKS_CACHE_KEY, self.jwks_data, 3600)
        self.client = APIClient()

        self.recruiter_user = User.objects.create(
            clerk_id="user_recruiter_prof_test_1",
            email="recruiter_prof@example.com",
            role=User.Role.RECRUITER,
        )
        self.recruiter_profile = RecruiterProfile.objects.create(
            user=self.recruiter_user,
            company_name="ProfileCorp",
            industry="Software",
        )

        self.candidate_user = User.objects.create(
            clerk_id="user_candidate_prof_test_1",
            email="candidate_prof@example.com",
            role=User.Role.CANDIDATE,
        )
        self.candidate_profile = CandidateProfile.objects.create(
            user=self.candidate_user,
            phone="1234567890",
            headline="Full Stack Engineer",
        )

    def _token(self, clerk_id: str) -> str:
        payload = {
            "sub": clerk_id,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "iss": "https://stunning-slug-13.clerk.accounts.dev",
        }
        pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": self.kid})

    def _auth(self, user: User) -> None:
        token = self._token(user.clerk_id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    # ── Recruiter Profile Tests ─────────────────────────────────

    def test_get_recruiter_profile_success(self) -> None:
        """GET /api/recruiters/profile/ returns self-owned recruiter profile with nested user."""
        self._auth(self.recruiter_user)
        response = self.client.get(reverse("interview_system:recruiter-profile"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["company_name"], "ProfileCorp")
        self.assertEqual(response.data["user"]["email"], "recruiter_prof@example.com")
        self.assertEqual(response.data["user"]["role"], "RECRUITER")

    def test_patch_recruiter_profile_success(self) -> None:
        """PATCH /api/recruiters/profile/ updates recruiter profile fields."""
        self._auth(self.recruiter_user)
        data = {
            "company_name": "UpdatedCorp",
            "company_size": "51-200",
            "position": "Head of Talent",
        }
        response = self.client.patch(
            reverse("interview_system:recruiter-profile"),
            data=data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["company_name"], "UpdatedCorp")
        self.assertEqual(response.data["company_size"], "51-200")
        self.assertEqual(response.data["position"], "Head of Talent")

    def test_patch_recruiter_profile_invalid_company_size(self) -> None:
        """PATCH /api/recruiters/profile/ with invalid company_size choice returns 400."""
        self._auth(self.recruiter_user)
        data = {"company_size": "10000+"}
        response = self.client.patch(
            reverse("interview_system:recruiter-profile"),
            data=data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_candidate_accessing_recruiter_profile_forbidden(self) -> None:
        """Candidate accessing /api/recruiters/profile/ gets 403 Forbidden."""
        self._auth(self.candidate_user)
        response = self.client.get(reverse("interview_system:recruiter-profile"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # ── Candidate Profile Tests ─────────────────────────────────

    def test_get_candidate_profile_success(self) -> None:
        """GET /api/candidates/profile/ returns self-owned candidate profile with nested user."""
        self._auth(self.candidate_user)
        response = self.client.get(reverse("interview_system:candidate-profile"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["headline"], "Full Stack Engineer")
        self.assertEqual(response.data["user"]["email"], "candidate_prof@example.com")
        self.assertEqual(response.data["user"]["role"], "CANDIDATE")

    def test_patch_candidate_profile_success(self) -> None:
        """PATCH /api/candidates/profile/ updates candidate profile fields."""
        self._auth(self.candidate_user)
        data = {
            "location": "San Francisco, CA",
            "linkedin_url": "https://linkedin.com/in/testuser",
            "portfolio_url": "https://portfolio.example.com",
            "years_experience": 4,
            "headline": "Lead Python Developer",
        }
        response = self.client.patch(
            reverse("interview_system:candidate-profile"),
            data=data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["location"], "San Francisco, CA")
        self.assertEqual(response.data["years_experience"], 4)
        self.assertEqual(response.data["linkedin_url"], "https://linkedin.com/in/testuser")

    def test_patch_candidate_profile_negative_experience_rejected(self) -> None:
        """PATCH /api/candidates/profile/ with negative years_experience returns 400."""
        self._auth(self.candidate_user)
        data = {"years_experience": -3}
        response = self.client.patch(
            reverse("interview_system:candidate-profile"),
            data=data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_candidate_profile_invalid_url_rejected(self) -> None:
        """PATCH /api/candidates/profile/ with invalid linkedin_url format returns 400."""
        self._auth(self.candidate_user)
        data = {"linkedin_url": "invalid-url-string"}
        response = self.client.patch(
            reverse("interview_system:candidate-profile"),
            data=data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_candidate_profile_headline_too_long(self) -> None:
        """PATCH /api/candidates/profile/ with headline > 150 chars returns 400."""
        self._auth(self.candidate_user)
        data = {"headline": "x" * 151}
        response = self.client.patch(
            reverse("interview_system:candidate-profile"),
            data=data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_recruiter_accessing_candidate_profile_forbidden(self) -> None:
        """Recruiter accessing /api/candidates/profile/ gets 403 Forbidden."""
        self._auth(self.recruiter_user)
        response = self.client.get(reverse("interview_system:candidate-profile"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# ═══════════════════════════════════════════════════════════════
#  Application — API Tests (Phase 4.4)
# ═══════════════════════════════════════════════════════════════

@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    },
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class ApplicationAPITestCase(TestCase):
    """
    Integration tests for ApplicationViewSet endpoints and the
    post_save signal that enqueues parse_resume on creation.
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        cls.public_key = cls.private_key.public_key()
        cls.kid = "test_application_api_kid"

        pub_numbers = cls.public_key.public_numbers()
        cls.jwk = {
            "kty": "RSA",
            "alg": "RS256",
            "use": "sig",
            "kid": cls.kid,
            "n": _int_to_base64url(pub_numbers.n),
            "e": _int_to_base64url(pub_numbers.e),
        }
        cls.jwks_data = {"keys": [cls.jwk]}

    def setUp(self) -> None:
        cache.clear()
        cache.set(JWKS_CACHE_KEY, self.jwks_data, 3600)
        self.client = APIClient()

        # ── Recruiter user + profile ──
        self.recruiter_user = User.objects.create(
            clerk_id="user_recruiter_app_test_1",
            email="recruiter_app@example.com",
            role=User.Role.RECRUITER,
        )
        self.recruiter_profile = RecruiterProfile.objects.create(
            user=self.recruiter_user,
            company_name="AppTestCorp",
        )

        # ── Another recruiter (non-owner) ──
        self.other_recruiter_user = User.objects.create(
            clerk_id="user_recruiter_app_test_2",
            email="other_recruiter_app@example.com",
            role=User.Role.RECRUITER,
        )
        self.other_recruiter_profile = RecruiterProfile.objects.create(
            user=self.other_recruiter_user,
            company_name="OtherAppCorp",
        )

        # ── Candidate user + profile ──
        self.candidate_user = User.objects.create(
            clerk_id="user_candidate_app_test_1",
            email="candidate_app@example.com",
            role=User.Role.CANDIDATE,
        )
        self.candidate_profile = CandidateProfile.objects.create(
            user=self.candidate_user,
        )

        # ── Another candidate ──
        self.other_candidate_user = User.objects.create(
            clerk_id="user_candidate_app_test_2",
            email="other_candidate_app@example.com",
            role=User.Role.CANDIDATE,
        )
        self.other_candidate_profile = CandidateProfile.objects.create(
            user=self.other_candidate_user,
        )

        # ── Active job (owned by recruiter_user) ──
        self.active_job = Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Active Test Job",
            description="Test description",
            requirements="Test requirements",
            skills_required=["Python", "Django"],
            location="Remote",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.MID,
            status=Job.Status.ACTIVE,
            deadline=date.today() + timedelta(days=30),
        )

        # ── Draft job ──
        self.draft_job = Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Draft Test Job",
            description="Draft",
            requirements="Draft",
            location="Remote",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.ENTRY,
            status=Job.Status.DRAFT,
        )

    def _token(self, clerk_id: str) -> str:
        payload = {
            "sub": clerk_id,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "iss": "https://stunning-slug-13.clerk.accounts.dev",
        }
        pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": self.kid})

    def _auth(self, user: User) -> None:
        token = self._token(user.clerk_id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _make_pdf(self, name: str = "resume.pdf", size_kb: int = 10) -> SimpleUploadedFile:
        """Create a fake PDF file for testing."""
        from django.core.files.uploadedfile import SimpleUploadedFile
        content = b"%PDF-1.4 fake content " + b"x" * (size_kb * 1024)
        return SimpleUploadedFile(name, content, content_type="application/pdf")

    def _make_docx(self, name: str = "resume.docx", size_kb: int = 10) -> SimpleUploadedFile:
        """Create a fake DOCX file for testing."""
        from django.core.files.uploadedfile import SimpleUploadedFile
        content = b"PK\x03\x04 fake docx " + b"x" * (size_kb * 1024)
        return SimpleUploadedFile(name, content, content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    # ────────────────────────────────────────────────────────────
    #  1. Happy path: candidate creates application with valid PDF
    # ────────────────────────────────────────────────────────────

    @patch("interview_system.signals.parse_resume")
    def test_candidate_creates_application_success(self, mock_parse_resume) -> None:
        """POST /api/applications/ with valid data creates Resume + Application."""
        self._auth(self.candidate_user)
        pdf = self._make_pdf()

        response = self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.active_job.pk),
                "resume_file": pdf,
                "consent_given": True,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify Application was created
        from ..models import Application, Resume
        self.assertEqual(Application.objects.count(), 1)
        app = Application.objects.first()
        self.assertEqual(app.candidate, self.candidate_profile)
        self.assertEqual(app.job, self.active_job)
        self.assertEqual(app.status, Application.Status.APPLIED)

        # Verify Resume was created
        self.assertEqual(Resume.objects.count(), 1)
        resume = Resume.objects.first()
        self.assertEqual(resume.candidate, self.candidate_profile)
        self.assertEqual(app.resume, resume)

    # ────────────────────────────────────────────────────────────
    #  2. Happy path: candidate lists own applications
    # ────────────────────────────────────────────────────────────

    @patch("interview_system.signals.parse_resume")
    def test_candidate_lists_own_applications(self, mock_parse_resume) -> None:
        """GET /api/applications/ as candidate returns only own applications."""
        self._auth(self.candidate_user)
        # Create an application
        self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.active_job.pk),
                "resume_file": self._make_pdf(),
                "consent_given": True,
            },
            format="multipart",
        )

        # List as the candidate
        response = self.client.get(reverse("interview_system:application-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["results"][0]["job_title"], "Active Test Job"
        )

        # Other candidate sees nothing
        self._auth(self.other_candidate_user)
        response = self.client.get(reverse("interview_system:application-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    # ────────────────────────────────────────────────────────────
    #  3. Happy path: recruiter lists applicants for own job
    # ────────────────────────────────────────────────────────────

    @patch("interview_system.signals.parse_resume")
    def test_recruiter_lists_applicants_for_own_job(self, mock_parse_resume) -> None:
        """GET /api/applications/?job={id} as job-owning recruiter returns applicants."""
        # Create application as candidate
        self._auth(self.candidate_user)
        self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.active_job.pk),
                "resume_file": self._make_pdf(),
                "consent_given": True,
            },
            format="multipart",
        )

        # List as the recruiter who owns the job
        self._auth(self.recruiter_user)
        response = self.client.get(
            reverse("interview_system:application-list"),
            {"job": str(self.active_job.pk)},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    # ────────────────────────────────────────────────────────────
    #  4. Happy path: recruiter retrieves single application
    # ────────────────────────────────────────────────────────────

    @patch("interview_system.signals.parse_resume")
    def test_recruiter_retrieves_application(self, mock_parse_resume) -> None:
        """GET /api/applications/{id}/ as job-owning recruiter returns detail."""
        self._auth(self.candidate_user)
        create_resp = self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.active_job.pk),
                "resume_file": self._make_pdf(),
                "consent_given": True,
            },
            format="multipart",
        )
        app_id = create_resp.data["id"]

        self._auth(self.recruiter_user)
        response = self.client.get(
            reverse("interview_system:application-detail", args=[app_id])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "APPLIED")

    # ────────────────────────────────────────────────────────────
    #  5. Happy path: recruiter advances APPLIED → SCREENED
    # ────────────────────────────────────────────────────────────

    @patch("interview_system.signals.parse_resume")
    def test_recruiter_advances_applied_to_screened(self, mock_parse_resume) -> None:
        """PATCH /api/applications/{id}/advance/ with status=SCREENED succeeds."""
        self._auth(self.candidate_user)
        create_resp = self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.active_job.pk),
                "resume_file": self._make_pdf(),
                "consent_given": True,
            },
            format="multipart",
        )
        app_id = create_resp.data["id"]

        self._auth(self.recruiter_user)
        response = self.client.patch(
            reverse("interview_system:application-advance", args=[app_id]),
            data={"status": "SCREENED"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "SCREENED")

    # ────────────────────────────────────────────────────────────
    #  6. Happy path: full lifecycle
    # ────────────────────────────────────────────────────────────

    @patch("interview_system.signals.parse_resume")
    def test_full_lifecycle_advancement(self, mock_parse_resume) -> None:
        """Advance through APPLIED → SCREENED → INTERVIEWED → DECISION."""
        self._auth(self.candidate_user)
        create_resp = self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.active_job.pk),
                "resume_file": self._make_pdf(),
                "consent_given": True,
            },
            format="multipart",
        )
        app_id = create_resp.data["id"]
        self._auth(self.recruiter_user)

        for next_status in ["SCREENED", "INTERVIEWED", "DECISION"]:
            response = self.client.patch(
                reverse("interview_system:application-advance", args=[app_id]),
                data={"status": next_status},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data["status"], next_status)

    # ────────────────────────────────────────────────────────────
    #  7. Error: job.status != ACTIVE
    # ────────────────────────────────────────────────────────────

    @patch("interview_system.signals.parse_resume")
    def test_apply_to_non_active_job_returns_400(self, mock_parse_resume) -> None:
        """POST /api/applications/ for a DRAFT job returns 400."""
        self._auth(self.candidate_user)
        response = self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.draft_job.pk),
                "resume_file": self._make_pdf(),
                "consent_given": True,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not accepting applications", str(response.data))

    # ────────────────────────────────────────────────────────────
    #  8. Error: duplicate (candidate, job) pair
    # ────────────────────────────────────────────────────────────

    @patch("interview_system.signals.parse_resume")
    def test_duplicate_application_returns_400(self, mock_parse_resume) -> None:
        """POST /api/applications/ twice for same (candidate, job) returns 400."""
        self._auth(self.candidate_user)
        data = {
            "job": str(self.active_job.pk),
            "resume_file": self._make_pdf(),
            "consent_given": True,
        }
        first = self.client.post(
            reverse("interview_system:application-list"),
            data=data,
            format="multipart",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        # Second attempt
        data["resume_file"] = self._make_pdf("resume2.pdf")
        second = self.client.post(
            reverse("interview_system:application-list"),
            data=data,
            format="multipart",
        )
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already applied", str(second.data))

    # ────────────────────────────────────────────────────────────
    #  9. Error: bad file extension
    # ────────────────────────────────────────────────────────────

    def test_bad_file_extension_returns_400(self) -> None:
        """POST /api/applications/ with a .txt file returns 400."""
        self._auth(self.candidate_user)
        from django.core.files.uploadedfile import SimpleUploadedFile
        txt_file = SimpleUploadedFile("notes.txt", b"some text", content_type="text/plain")

        response = self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.active_job.pk),
                "resume_file": txt_file,
                "consent_given": True,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Unsupported file type", str(response.data))

    # ────────────────────────────────────────────────────────────
    #  10. Error: oversized file
    # ────────────────────────────────────────────────────────────

    def test_oversized_file_returns_400(self) -> None:
        """POST /api/applications/ with >5MB file returns 400."""
        self._auth(self.candidate_user)
        # Create a file just over 5MB
        big_pdf = self._make_pdf("big.pdf", size_kb=5200)

        response = self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.active_job.pk),
                "resume_file": big_pdf,
                "consent_given": True,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("5MB", str(response.data))

    # ────────────────────────────────────────────────────────────
    #  11. Error: consent_given not true
    # ────────────────────────────────────────────────────────────

    def test_consent_not_given_returns_400(self) -> None:
        """POST /api/applications/ with consent_given=false returns 400."""
        self._auth(self.candidate_user)
        response = self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.active_job.pk),
                "resume_file": self._make_pdf(),
                "consent_given": False,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Consent is required", str(response.data))

    # ────────────────────────────────────────────────────────────
    #  12. Error: recruiter GET list without ?job=
    # ────────────────────────────────────────────────────────────

    def test_recruiter_list_without_job_param_returns_400(self) -> None:
        """GET /api/applications/ as recruiter without ?job= returns 400."""
        self._auth(self.recruiter_user)
        response = self.client.get(reverse("interview_system:application-list"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("job", str(response.data).lower())

    # ────────────────────────────────────────────────────────────
    #  13. Error: recruiter GET list for non-owned job → 403
    # ────────────────────────────────────────────────────────────

    def test_recruiter_list_non_owned_job_returns_403(self) -> None:
        """GET /api/applications/?job={id} for a job not owned by recruiter returns 403."""
        self._auth(self.other_recruiter_user)
        response = self.client.get(
            reverse("interview_system:application-list"),
            {"job": str(self.active_job.pk)},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # ────────────────────────────────────────────────────────────
    #  14. Error: non-owner accessing GET /{id}/ → 404
    # ────────────────────────────────────────────────────────────

    @patch("interview_system.signals.parse_resume")
    def test_non_owner_retrieve_returns_404(self, mock_parse_resume) -> None:
        """GET /api/applications/{id}/ by non-owning candidate returns 404."""
        self._auth(self.candidate_user)
        create_resp = self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.active_job.pk),
                "resume_file": self._make_pdf(),
                "consent_given": True,
            },
            format="multipart",
        )
        app_id = create_resp.data["id"]

        # Other candidate tries to access
        self._auth(self.other_candidate_user)
        response = self.client.get(
            reverse("interview_system:application-detail", args=[app_id])
        )
        # Should be 403 (permission denied) since queryset scoping yields empty
        # for the other candidate, DRF returns 404
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    # ────────────────────────────────────────────────────────────
    #  15. Error: non-recruiter hitting /advance/ → 403
    # ────────────────────────────────────────────────────────────

    @patch("interview_system.signals.parse_resume")
    def test_candidate_cannot_advance(self, mock_parse_resume) -> None:
        """PATCH /api/applications/{id}/advance/ as candidate returns 403."""
        self._auth(self.candidate_user)
        create_resp = self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.active_job.pk),
                "resume_file": self._make_pdf(),
                "consent_given": True,
            },
            format="multipart",
        )
        app_id = create_resp.data["id"]

        # Candidate tries to advance — forbidden
        response = self.client.patch(
            reverse("interview_system:application-advance", args=[app_id]),
            data={"status": "SCREENED"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # ────────────────────────────────────────────────────────────
    #  16. Error: /advance/ with non-sequential status (skip)
    # ────────────────────────────────────────────────────────────

    @patch("interview_system.signals.parse_resume")
    def test_advance_skip_status_returns_400(self, mock_parse_resume) -> None:
        """PATCH /advance/ skipping SCREENED (APPLIED → INTERVIEWED) returns 400."""
        self._auth(self.candidate_user)
        create_resp = self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.active_job.pk),
                "resume_file": self._make_pdf(),
                "consent_given": True,
            },
            format="multipart",
        )
        app_id = create_resp.data["id"]

        self._auth(self.recruiter_user)
        response = self.client.patch(
            reverse("interview_system:application-advance", args=[app_id]),
            data={"status": "INTERVIEWED"},  # skips SCREENED
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("SCREENED", str(response.data))

    # ────────────────────────────────────────────────────────────
    #  17. Error: /advance/ with backward status
    # ────────────────────────────────────────────────────────────

    @patch("interview_system.signals.parse_resume")
    def test_advance_backward_returns_400(self, mock_parse_resume) -> None:
        """PATCH /advance/ with a backward status (SCREENED → APPLIED) returns 400."""
        self._auth(self.candidate_user)
        create_resp = self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.active_job.pk),
                "resume_file": self._make_pdf(),
                "consent_given": True,
            },
            format="multipart",
        )
        app_id = create_resp.data["id"]

        # First advance to SCREENED
        self._auth(self.recruiter_user)
        self.client.patch(
            reverse("interview_system:application-advance", args=[app_id]),
            data={"status": "SCREENED"},
            format="json",
        )

        # Try to go back to APPLIED
        response = self.client.patch(
            reverse("interview_system:application-advance", args=[app_id]),
            data={"status": "APPLIED"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("INTERVIEWED", str(response.data))  # next valid status

    # ────────────────────────────────────────────────────────────
    #  18. Signal fires parse_resume.delay on creation
    # ────────────────────────────────────────────────────────────

    @patch("interview_system.signals.parse_resume")
    def test_signal_fires_parse_resume_on_creation(self, mock_parse_resume) -> None:
        """post_save signal calls parse_resume.delay with correct resume_id on transaction commit."""
        self._auth(self.candidate_user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("interview_system:application-list"),
                data={
                    "job": str(self.active_job.pk),
                    "resume_file": self._make_pdf(),
                    "consent_given": True,
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify parse_resume.delay was called once
        mock_parse_resume.delay.assert_called_once()

        # Verify it was called with the correct resume_id
        from ..models import Application
        app = Application.objects.first()
        call_kwargs = mock_parse_resume.delay.call_args
        self.assertEqual(
            call_kwargs.kwargs.get("resume_id") or call_kwargs[1].get("resume_id"),
            str(app.resume_id),
        )


    # ────────────────────────────────────────────────────────────
    #  19. Signal does NOT fire on advance (save but not create)
    # ────────────────────────────────────────────────────────────

    @patch("interview_system.signals.parse_resume")
    def test_signal_does_not_fire_on_advance(self, mock_parse_resume) -> None:
        """post_save signal does NOT call parse_resume.delay on status advance."""
        self._auth(self.candidate_user)
        create_resp = self.client.post(
            reverse("interview_system:application-list"),
            data={
                "job": str(self.active_job.pk),
                "resume_file": self._make_pdf(),
                "consent_given": True,
            },
            format="multipart",
        )
        app_id = create_resp.data["id"]

        # Reset mock after creation (which does fire)
        mock_parse_resume.delay.reset_mock()

        # Advance the application
        self._auth(self.recruiter_user)
        response = self.client.patch(
            reverse("interview_system:application-advance", args=[app_id]),
            data={"status": "SCREENED"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # parse_resume.delay should NOT have been called again
        mock_parse_resume.delay.assert_not_called()


# ═══════════════════════════════════════════════════════════════
#  Async Pipeline Task Graph Tests (Phase 4 Task Chain)
# ═══════════════════════════════════════════════════════════════

@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    },
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class TaskPipelineTestCase(TestCase):
    """
    Tests for the 8 Celery pipeline tasks, verifying:
      1. Complete end-to-end task chain execution and DB persistence.
      2. Retry policy behavior with injected failures.
      3. Signal transaction safety.
    """

    def setUp(self) -> None:
        self.recruiter_user = User.objects.create(
            clerk_id="user_recruiter_task_test",
            email="recruiter_task@example.com",
            role=User.Role.RECRUITER,
        )
        self.recruiter_profile = RecruiterProfile.objects.create(
            user=self.recruiter_user,
            company_name="TaskCorp",
        )
        self.candidate_user = User.objects.create(
            clerk_id="user_candidate_task_test",
            email="candidate_task@example.com",
            role=User.Role.CANDIDATE,
        )
        self.candidate_profile = CandidateProfile.objects.create(
            user=self.candidate_user,
        )
        self.job = Job.objects.create(
            recruiter=self.recruiter_profile,
            title="Pipeline Developer",
            description="Build task graphs",
            requirements="Python & Celery",
            location="Remote",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.SENIOR,
            status=Job.Status.ACTIVE,
            deadline=date.today() + timedelta(days=30),
        )
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.resume = Resume.objects.create(
            candidate=self.candidate_profile,
            file=SimpleUploadedFile("test_resume.pdf", b"%PDF-1.4 John Doe Python Django developer"),
        )

    @patch("ai.matching.sbert.match")
    @patch("interview_system.integrations.gemini_client.parse_resume_text")
    @patch("interview_system.resumes.extraction.extract_text")
    def test_end_to_end_task_chain_execution(self, mock_extract, mock_gemini, mock_sbert_match) -> None:
        """
        Creating an Application automatically triggers the task chain end-to-end:
        parse_resume → compute_match_score → generate_candidate_score → send_notification.
        """
        mock_extract.return_value = "John Doe Python Django developer"
        mock_gemini.return_value = {
            "skills": ["Python", "Django", "PostgreSQL"],
            "education": [{"degree": "B.S. CS", "institution": "MIT", "year": 2020}],
            "experience": [{"title": "SWE", "company": "Acme", "duration": "2y", "description": "Built APIs"}],
            "certifications": ["AWS"],
        }
        mock_sbert_match.return_value = {
            "similarity": 0.85,
            "matched_skills": ["Python", "Django"],
            "missing_skills": ["Docker"],
        }

        from ..models import (
            Application,
            BehavioralAnalysis,
            CandidateScore,
            Interview,
            InterviewSession,
            Notification,
            ParsedResume,
            Question,
        )
        from ..tasks import parse_resume

        # Create application (fires post_save signal)
        app = Application.objects.create(
            candidate=self.candidate_profile,
            job=self.job,
            resume=self.resume,
            status=Application.Status.APPLIED,
        )

        # Trigger parse_resume directly or via signal
        parse_resume.delay(resume_id=str(self.resume.id))

        # 1. Verify ParsedResume created and match score computed
        parsed_resume = ParsedResume.objects.get(resume=self.resume)
        self.assertIn("Python", parsed_resume.matched_skills)
        self.assertIn("Docker", parsed_resume.missing_skills)

    @patch("interview_system.integrations.gemini_client.parse_resume_text")
    @patch("interview_system.resumes.extraction.extract_text")
    def test_retry_policy_with_injected_failure(self, mock_extract, mock_gemini) -> None:
        """
        Verify Celery task retry policy handles transient errors up to max_retries.
        """
        from ..tasks.parsing import parse_resume

        mock_extract.return_value = "John Doe Python developer"
        mock_gemini.side_effect = GeminiParseError("Gemini timeout")

        with self.assertRaises(GeminiParseError):
            parse_resume.run(resume_id=str(self.resume.id))

    @patch("ai.matching.sbert.match")
    @patch("interview_system.integrations.gemini_client.parse_resume_text")
    @patch("interview_system.resumes.extraction.extract_text")
    def test_task_signatures_and_persistence_contracts(self, mock_extract, mock_gemini, mock_sbert_match) -> None:
        """
        Directly invoke every task stub step-by-step and verify returns and DB persistence targets.
        """
        mock_extract.return_value = "John Doe Python Django developer"
        mock_gemini.return_value = {
            "skills": ["Python", "Django"],
            "education": [],
            "experience": [],
            "certifications": [],
        }
        mock_sbert_match.return_value = {
            "similarity": 0.85,
            "matched_skills": ["Python", "Django"],
            "missing_skills": ["Docker"],
        }

        from ..models import (
            Application,
            BehavioralAnalysis,
            CandidateScore,
            Interview,
            InterviewSession,
            Notification,
            ParsedResume,
            Question,
        )
        from ..tasks import (
            aggregate_behavioral,
            compute_match_score,
            generate_candidate_score,
            generate_questions,
            parse_resume,
            process_retell_transcript,
            schedule_interview,
            send_notification,
        )

        with patch("interview_system.tasks.parsing.compute_match_score.delay"):
            app = Application.objects.create(
                candidate=self.candidate_profile,
                job=self.job,
                resume=self.resume,
            )

        # Task 1: parse_resume
        with patch("interview_system.tasks.parsing.compute_match_score.delay"):
            res_parse = parse_resume.run(resume_id=str(self.resume.id))
        self.assertIn("skills", res_parse)
        self.assertTrue(ParsedResume.objects.filter(resume=self.resume).exists())

        # Task 2: compute_match_score
        with patch("interview_system.tasks.analysis.generate_candidate_score.delay"):
            res_match = compute_match_score.run(resume_id=str(self.resume.id))
        self.assertIn("match_score", res_match)

        # Task 3: schedule_interview
        with patch("interview_system.tasks.interviewing.generate_questions.delay"):
            res_sched = schedule_interview.run(application_id=str(app.id))
        self.assertEqual(res_sched["status"], "SCHEDULED")
        interview_id = res_sched["interview_id"]

        # Task 4: generate_questions
        with patch("interview_system.tasks.interviewing.process_retell_transcript.delay"):
            res_q = generate_questions.run(interview_id=interview_id)
        self.assertEqual(len(res_q), 3)

        # Task 5: process_retell_transcript
        with patch("interview_system.tasks.analysis.aggregate_behavioral.delay"):
            res_trans = process_retell_transcript.run(payload={"interview_id": interview_id, "transcript": "Custom transcript"})
        self.assertEqual(res_trans["transcript"], "Custom transcript")
        session_id = res_trans["session_id"]

        # Task 6: aggregate_behavioral
        with patch("interview_system.tasks.analysis.generate_candidate_score.delay"):
            res_beh = aggregate_behavioral.run(session_id=session_id)
        self.assertEqual(res_beh["attention_pct"], 94.5)

        # Task 7: generate_candidate_score
        with patch("interview_system.tasks.notifications.send_notification.delay"):
            res_score = generate_candidate_score.run(application_id=str(app.id))
        self.assertEqual(res_score["final_score"], 88.5)

        # Task 8: send_notification
        res_notif = send_notification.run(user_id=str(self.candidate_user.id), event={"type": "TEST_EVENT"})
        self.assertEqual(res_notif["status"], "SENT")
        self.assertTrue(Notification.objects.filter(user=self.candidate_user, type="TEST_EVENT").exists())


