"""Regression tests for review findings R06–R10."""

import io
import json
import tempfile
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient, APITestCase

from ai.matching.sbert import match
from interview_system.integrations.gemini_client import parse_resume_text
from interview_system.models import Application, CandidateProfile, Job, JobSkill, RecruiterProfile, Resume, User


class EmbeddingCacheIntegrationTests(SimpleTestCase):
    def test_repeated_and_one_sided_matches_reuse_embeddings(self):
        cache = {}
        redis = MagicMock()
        redis.get.side_effect = lambda key: cache.get(key)
        redis.setex.side_effect = lambda key, ttl, value: cache.__setitem__(key, value)
        model = MagicMock()
        model.encode.side_effect = lambda text: [float(len(text)), 1.0]
        with patch("ai.matching.cache._get_redis_client", return_value=redis), patch(
            "ai.matching.sbert._get_model", return_value=model
        ):
            first = match("Python resume", "Python job", ["Python"])
            self.assertEqual(match("Python resume", "Python job", ["Python"]), first)
            self.assertEqual(model.encode.call_count, 2)
            match("Python resume", "Different job", ["Python"])
            self.assertEqual(model.encode.call_count, 3)
            self.assertEqual(len(cache), 3)


class NextPhaseAPITests(APITestCase):
    def setUp(self):
        media = tempfile.TemporaryDirectory(prefix="skillsense-next-test-")
        self.addCleanup(media.cleanup)
        settings_override = override_settings(MEDIA_ROOT=media.name)
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        self.owner = User.objects.create(clerk_id="next_owner", email="next_owner@example.test", role="RECRUITER")
        self.candidate = User.objects.create(clerk_id="next_candidate", email="next_candidate@example.test", role="CANDIDATE")
        self.profile = RecruiterProfile.objects.create(user=self.owner, company_name="Example")
        self.candidate_profile = CandidateProfile.objects.create(user=self.candidate)
        self.job = Job.objects.create(
            recruiter=self.profile, title="Engineer", description="Build services", requirements="Python",
            skills_required=["Python"], location="Remote", job_type="REMOTE",
            experience_level="ENTRY", status=Job.Status.ACTIVE,
            deadline=date.today() + timedelta(days=30),
        )
        self.client = APIClient()

    def test_nested_skill_changes_update_matching_input(self):
        self.client.force_authenticate(self.owner)
        base = f"/api/jobs/{self.job.pk}/skills/"
        created = self.client.post(base, {"skill_name": "Rust", "is_required": True})
        self.assertEqual(created.status_code, 201)
        self.job.refresh_from_db()
        self.assertEqual(self.job.skills_required, ["Python", "Rust"])
        detail = f"{base}{created.data['id']}/"
        changed = self.client.patch(detail, {"skill_name": "Go", "is_required": False})
        self.assertEqual(changed.status_code, 200)
        self.job.refresh_from_db()
        self.assertEqual(self.job.skills_required, ["Python"])
        self.assertEqual(self.client.delete(detail).status_code, 204)
        self.job.refresh_from_db()
        self.assertEqual(self.job.skills_required, ["Python"])

    def test_array_update_preserves_existing_ids_and_optional_skills(self):
        required = JobSkill.objects.create(job=self.job, skill_name="Python", is_required=True)
        optional = JobSkill.objects.create(job=self.job, skill_name="Docker", is_required=False)
        self.client.force_authenticate(self.owner)
        response = self.client.patch(f"/api/jobs/{self.job.pk}/", {"skills_required": ["Python", "Rust"]}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(JobSkill.objects.filter(pk=required.pk, is_required=True).exists())
        self.assertTrue(JobSkill.objects.filter(pk=optional.pk, is_required=False).exists())
        self.assertTrue(JobSkill.objects.filter(job=self.job, skill_name="Rust", is_required=True).exists())
        self.job.refresh_from_db()
        self.assertEqual(self.job.skills_required, ["Python", "Rust"])

    def test_optional_skill_creation_clears_legacy_array_only_requirement(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            f"/api/jobs/{self.job.pk}/skills/",
            {"skill_name": "Python", "is_required": False},
        )
        self.assertEqual(response.status_code, 201)
        self.job.refresh_from_db()
        self.assertEqual(self.job.skills_required, [])

    def test_duplicate_skill_and_malformed_job_filter_are_client_errors(self):
        JobSkill.objects.create(job=self.job, skill_name="Python")
        self.client.force_authenticate(self.owner)
        duplicate = self.client.post(f"/api/jobs/{self.job.pk}/skills/", {"skill_name": "Python"})
        self.assertEqual(duplicate.status_code, 400)
        invalid = self.client.get("/api/applications/?job=not-a-uuid")
        self.assertEqual(invalid.status_code, 400)

    def test_submitted_resume_delete_returns_conflict(self):
        resume = Resume.objects.create(candidate=self.candidate_profile, file="resumes/submitted.pdf")
        Application.objects.create(candidate=self.candidate_profile, job=self.job, resume=resume)
        self.client.force_authenticate(self.candidate)
        result = self.client.delete(f"/api/candidates/resumes/{resume.pk}/")
        self.assertEqual(result.status_code, 409)
        self.assertTrue(Resume.objects.filter(pk=resume.pk).exists())

    def test_external_logo_round_trips_and_can_be_cleared(self):
        self.client.force_authenticate(self.owner)
        url = "/api/recruiters/profile/"
        external = "https://example.test/logo.png"
        saved = self.client.patch(url, {"company_logo": external}, format="json")
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.data["company_logo"], external)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.company_logo_url, external)
        self.assertFalse(self.profile.company_logo)
        self.assertEqual(self.client.get(url).data["company_logo"], external)
        cleared = self.client.patch(url, {"company_logo": ""}, format="json")
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(cleared.data["company_logo"])

        from PIL import Image

        stream = io.BytesIO()
        Image.new("RGB", (2, 2), "blue").save(stream, format="PNG")
        uploaded = self.client.patch(
            url, {"company_logo": SimpleUploadedFile("logo.png", stream.getvalue(), content_type="image/png")},
            format="multipart",
        )
        self.assertEqual(uploaded.status_code, 200)
        self.assertIn("/media/recruiter/logos/", uploaded.data["company_logo"])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.company_logo_url, "")
        self.assertTrue(self.profile.company_logo)
        cleared_upload = self.client.patch(url, {"company_logo": None}, format="json")
        self.assertEqual(cleared_upload.status_code, 200)
        self.assertIsNone(cleared_upload.data["company_logo"])


class GeminiConfigurationTests(SimpleTestCase):
    @override_settings(GEMINI_API_KEY="test-key", GEMINI_MODEL_NAME="chosen-model", GEMINI_REQUEST_TIMEOUT_MS=12345)
    @patch("google.genai.Client")
    def test_model_and_timeout_come_from_settings(self, client_class):
        client_class.return_value.models.generate_content.return_value.text = json.dumps({
            "skills": [], "education": [], "experience": [], "certifications": [],
        })
        self.assertEqual(parse_resume_text("Resume text")["skills"], [])
        self.assertEqual(client_class.call_args.kwargs["http_options"].timeout, 12345)
        self.assertEqual(client_class.call_args.kwargs["http_options"].retry_options.attempts, 1)
        self.assertEqual(client_class.return_value.models.generate_content.call_args.kwargs["model"], "chosen-model")
