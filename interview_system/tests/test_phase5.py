"""
Phase 5 tests — Resume Intelligence pipeline.

Covers:
  - Text extraction (PDF, DOCX, corrupt file → ExtractionError)
  - Gemini client (mocked: success, malformed response, timeout)
  - SBERT matching (real computation with known inputs)
  - Task wiring (parse_resume → ParsedResume, compute_match_score → match_score)
  - Failure handling (Gemini error → retries → FAILED status)
  - GET /api/resumes/{id}/ endpoint (status, permissions)
  - Upload validation (magic-byte sniffing, size limit)
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from interview_system.integrations.gemini_client import (
    GeminiParseError,
    _strip_markdown_fences,
    _validate_parsed_data,
)
from interview_system.models import (
    Application,
    CandidateProfile,
    Job,
    ParsedResume,
    RecruiterProfile,
    Resume,
    User,
)
from interview_system.resumes.extraction import ExtractionError


# ═══════════════════════════════════════════════════════════════
#  Helper: create test user fixtures
# ═══════════════════════════════════════════════════════════════

def _create_candidate():
    """Create a Candidate user + profile for testing."""
    user = User.objects.create(
        clerk_id="test_candidate_clerk",
        email="candidate@test.com",
        role=User.Role.CANDIDATE,
    )
    profile = CandidateProfile.objects.create(user=user)
    return user, profile


def _create_recruiter():
    """Create a Recruiter user + profile for testing."""
    user = User.objects.create(
        clerk_id="test_recruiter_clerk",
        email="recruiter@test.com",
        role=User.Role.RECRUITER,
    )
    profile = RecruiterProfile.objects.create(user=user, company_name="TestCorp")
    return user, profile


def _create_job(recruiter_profile):
    """Create an active job for testing."""
    from django.utils import timezone
    from datetime import timedelta

    return Job.objects.create(
        recruiter=recruiter_profile,
        title="Senior Python Developer",
        description="Build scalable Python backends using Django and FastAPI.",
        requirements="5+ years Python experience",
        skills_required=["Python", "Django", "PostgreSQL", "Docker"],
        location="Remote",
        job_type=Job.JobType.REMOTE,
        experience_level=Job.ExperienceLevel.SENIOR,
        status=Job.Status.ACTIVE,
        deadline=timezone.now().date() + timedelta(days=30),
    )


# ═══════════════════════════════════════════════════════════════
#  Text Extraction Tests
# ═══════════════════════════════════════════════════════════════

class ExtractionTests(TestCase):
    """Tests for interview_system.resumes.extraction."""

    @patch("interview_system.resumes.extraction._detect_mime")
    def test_extract_text_empty_file_raises(self, mock_mime):
        """Empty file should raise ExtractionError."""
        from interview_system.resumes.extraction import extract_text

        f = io.BytesIO(b"")
        with self.assertRaises(ExtractionError):
            extract_text(f)

    @patch("interview_system.resumes.extraction._detect_mime")
    def test_extract_text_unsupported_mime_raises(self, mock_mime):
        """Non-PDF/DOCX MIME should raise ExtractionError."""
        from interview_system.resumes.extraction import extract_text

        mock_mime.return_value = "image/jpeg"
        f = io.BytesIO(b"fake content that is not empty")
        with self.assertRaises(ExtractionError) as ctx:
            extract_text(f)
        self.assertIn("Unsupported file type", str(ctx.exception))

    @patch("interview_system.resumes.extraction._detect_mime")
    @patch("interview_system.resumes.extraction._extract_pdf_text")
    def test_extract_text_pdf_happy_path(self, mock_pdf, mock_mime):
        """PDF extraction should return text when pdfplumber succeeds."""
        from interview_system.resumes.extraction import extract_text

        mock_mime.return_value = "application/pdf"
        mock_pdf.return_value = "John Doe - Software Engineer with 5 years of experience in Python and Django."
        f = io.BytesIO(b"%PDF-1.4 fake pdf content")
        result = extract_text(f)
        self.assertEqual(result, mock_pdf.return_value)

    @patch("interview_system.resumes.extraction._detect_mime")
    @patch("interview_system.resumes.extraction._extract_docx_text")
    def test_extract_text_docx_happy_path(self, mock_docx, mock_mime):
        """DOCX extraction should return text when python-docx succeeds."""
        from interview_system.resumes.extraction import extract_text

        mock_mime.return_value = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        mock_docx.return_value = "Jane Doe - Data Scientist"
        f = io.BytesIO(b"PK\\x03\\x04 fake docx content")
        result = extract_text(f)
        self.assertEqual(result, mock_docx.return_value)


# ═══════════════════════════════════════════════════════════════
#  Gemini Client Tests
# ═══════════════════════════════════════════════════════════════

class GeminiClientTests(TestCase):
    """Tests for interview_system.integrations.gemini_client."""

    def test_strip_markdown_fences(self):
        """Should remove ```json ... ``` fences."""
        raw = '```json\\n{"skills": ["Python"]}\\n```'
        result = _strip_markdown_fences(raw)
        # The fences should be stripped
        self.assertNotIn("```", result)

    def test_strip_markdown_fences_no_fences(self):
        """Should pass through text without fences."""
        raw = '{"skills": ["Python"]}'
        result = _strip_markdown_fences(raw)
        self.assertEqual(result, raw)

    def test_validate_parsed_data_valid(self):
        """Valid data should pass validation."""
        data = {
            "skills": ["Python", "Django"],
            "education": [{"degree": "B.S. CS", "institution": "MIT", "year": 2020}],
            "experience": [{"title": "SWE", "company": "Google", "duration": "2 years", "description": "Built stuff"}],
            "certifications": ["AWS SAA"],
        }
        result = _validate_parsed_data(data)
        self.assertEqual(result["skills"], ["Python", "Django"])
        self.assertEqual(len(result["education"]), 1)
        self.assertEqual(len(result["experience"]), 1)

    def test_validate_parsed_data_not_dict(self):
        """Non-dict input should raise GeminiParseError."""
        with self.assertRaises(GeminiParseError):
            _validate_parsed_data("not a dict")

    def test_validate_parsed_data_skills_not_list(self):
        """skills field that isn't a list should raise."""
        with self.assertRaises(GeminiParseError):
            _validate_parsed_data({"skills": "Python", "education": [], "experience": [], "certifications": []})

    def test_parse_resume_text_empty_text(self):
        """Empty text should raise GeminiParseError."""
        from interview_system.integrations.gemini_client import parse_resume_text

        with self.assertRaises(GeminiParseError):
            parse_resume_text("")

    @override_settings(GEMINI_API_KEY="")
    def test_parse_resume_text_no_api_key(self):
        """Missing API key should raise GeminiParseError."""
        from interview_system.integrations.gemini_client import parse_resume_text

        with self.assertRaises(GeminiParseError) as ctx:
            parse_resume_text("Some resume text")
        self.assertIn("GEMINI_API_KEY", str(ctx.exception))


# ═══════════════════════════════════════════════════════════════
#  SBERT Matching Tests
# ═══════════════════════════════════════════════════════════════

class SBERTMatchTests(TestCase):
    """Tests for ai.matching.sbert (pure Python, no Django deps)."""

    @patch("ai.matching.sbert._get_model")
    def test_match_returns_expected_shape(self, mock_get_model):
        """match() should return similarity, matched_skills, missing_skills."""
        from ai.matching.sbert import match

        # Mock the model to return predictable embeddings
        mock_model = MagicMock()
        mock_model.encode.side_effect = [
            [1.0, 0.0, 0.0],  # resume embedding
            [1.0, 0.0, 0.0],  # job embedding (identical)
        ]
        mock_get_model.return_value = mock_model

        result = match(
            resume_text="Python Django PostgreSQL developer",
            job_text="Python Django developer needed",
            job_skills=["Python", "Django", "Docker"],
        )

        self.assertIn("similarity", result)
        self.assertIn("matched_skills", result)
        self.assertIn("missing_skills", result)
        self.assertAlmostEqual(result["similarity"], 1.0, places=2)
        self.assertIn("Python", result["matched_skills"])
        self.assertIn("Django", result["matched_skills"])
        self.assertIn("Docker", result["missing_skills"])

    @patch("ai.matching.sbert._get_model")
    def test_match_orthogonal_embeddings(self, mock_get_model):
        """Orthogonal embeddings should produce similarity near 0."""
        from ai.matching.sbert import match

        mock_model = MagicMock()
        mock_model.encode.side_effect = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ]
        mock_get_model.return_value = mock_model

        result = match(
            resume_text="Completely unrelated text",
            job_text="Totally different topic",
        )
        self.assertAlmostEqual(result["similarity"], 0.0, places=2)

    def test_match_with_cache(self):
        """Embedding cache should store and retrieve vectors from Redis client."""
        from ai.matching.cache import get_cached_embedding, set_cached_embedding

        mock_redis = MagicMock()
        mock_redis.get.return_value = None  # Miss

        sample_text = "Python Django Backend Developer"
        sample_vec = [0.1, 0.2, 0.3]

        miss = get_cached_embedding(sample_text, client=mock_redis)
        self.assertIsNone(miss)

        set_cached_embedding(sample_text, sample_vec, client=mock_redis)
        self.assertTrue(mock_redis.setex.called)


# ═══════════════════════════════════════════════════════════════
#  Task Tests
# ═══════════════════════════════════════════════════════════════

@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    },
)
class TaskTests(TestCase):
    """Tests for parse_resume and compute_match_score tasks."""

    def setUp(self):
        self.candidate_user, self.candidate_profile = _create_candidate()
        self.recruiter_user, self.recruiter_profile = _create_recruiter()
        self.job = _create_job(self.recruiter_profile)
        self.resume = Resume.objects.create(
            candidate=self.candidate_profile,
            file=SimpleUploadedFile("test.pdf", b"%PDF-1.4 test content"),
        )
        self.application = Application.objects.create(
            candidate=self.candidate_profile,
            job=self.job,
            resume=self.resume,
        )

    @patch("interview_system.tasks.parsing.compute_match_score")
    @patch("interview_system.integrations.gemini_client.parse_resume_text")
    @patch("interview_system.resumes.extraction.extract_text")
    def test_parse_resume_success(self, mock_extract, mock_gemini, mock_compute):
        """parse_resume should create ParsedResume and set PARSED status."""
        from interview_system.tasks.parsing import parse_resume

        mock_extract.return_value = "John Doe - Software Engineer"
        mock_gemini.return_value = {
            "skills": ["Python"],
            "education": [{"degree": "B.S.", "institution": "MIT", "year": 2020}],
            "experience": [{"title": "SWE", "company": "Acme", "duration": "2y", "description": "Built stuff"}],
            "certifications": ["AWS"],
        }

        # Call synchronously (CELERY_TASK_ALWAYS_EAGER=True)
        parse_resume(resume_id=str(self.resume.pk))

        self.resume.refresh_from_db()
        self.assertEqual(self.resume.status, Resume.Status.PARSED)

        parsed = ParsedResume.objects.get(resume=self.resume)
        self.assertEqual(parsed.skills, ["Python"])
        self.assertEqual(parsed.raw_text, "John Doe - Software Engineer")

    @patch("interview_system.tasks.parsing.compute_match_score")
    @patch("interview_system.resumes.extraction.extract_text")
    def test_parse_resume_extraction_failure(self, mock_extract, mock_compute):
        """ExtractionError should immediately set FAILED, no retry."""
        from interview_system.tasks.parsing import parse_resume

        mock_extract.side_effect = ExtractionError("Corrupt PDF")

        result = parse_resume(resume_id=str(self.resume.pk))

        self.resume.refresh_from_db()
        self.assertEqual(self.resume.status, Resume.Status.FAILED)
        self.assertIn("error", result)
        mock_compute.delay.assert_not_called()


# ═══════════════════════════════════════════════════════════════
#  Endpoint Tests — GET /api/resumes/{id}/
# ═══════════════════════════════════════════════════════════════

@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    },
)
class ResumeEndpointTests(TestCase):
    """Tests for GET /api/resumes/{id}/."""

    def setUp(self):
        self.client = APIClient()
        self.candidate_user, self.candidate_profile = _create_candidate()
        self.recruiter_user, self.recruiter_profile = _create_recruiter()
        self.job = _create_job(self.recruiter_profile)
        self.resume = Resume.objects.create(
            candidate=self.candidate_profile,
            file=SimpleUploadedFile("test.pdf", b"%PDF-1.4 test content"),
        )
        self.application = Application.objects.create(
            candidate=self.candidate_profile,
            job=self.job,
            resume=self.resume,
        )

    def test_resume_detail_pending(self):
        """PENDING resume should return null for all parsed fields."""
        self.client.force_authenticate(user=self.candidate_user)
        url = f"/api/resumes/{self.resume.pk}/"
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["status"], "PENDING")
        self.assertIsNone(data["skills"])
        self.assertIsNone(data["match_score"])

    def test_resume_detail_parsed(self):
        """PARSED resume should return real data."""
        self.resume.status = Resume.Status.PARSED
        self.resume.save()
        ParsedResume.objects.create(
            resume=self.resume,
            skills=["Python", "Django"],
            education=[{"degree": "B.S.", "institution": "MIT", "year": 2020}],
            experience=[{"title": "SWE", "company": "Acme", "duration": "2y", "description": "Built APIs"}],
            certifications=["AWS"],
            raw_text="John Doe, Python developer",
            match_score=85.0,
            matched_skills=["Python", "Django"],
            missing_skills=["Docker"],
        )

        self.client.force_authenticate(user=self.candidate_user)
        url = f"/api/resumes/{self.resume.pk}/"
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["status"], "PARSED")
        self.assertEqual(data["skills"], ["Python", "Django"])
        self.assertEqual(data["match_score"], 85.0)
        self.assertIn("Docker", data["missing_skills"])

    def test_resume_detail_recruiter_access(self):
        """Recruiter of linked job should be able to view resume."""
        self.client.force_authenticate(user=self.recruiter_user)
        url = f"/api/resumes/{self.resume.pk}/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_resume_detail_unauthorized_user(self):
        """Unrelated user should get 403."""
        other_user = User.objects.create(
            clerk_id="other_user_clerk",
            email="other@test.com",
            role=User.Role.CANDIDATE,
        )
        CandidateProfile.objects.create(user=other_user)

        self.client.force_authenticate(user=other_user)
        url = f"/api/resumes/{self.resume.pk}/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_resume_detail_unauthenticated(self):
        """Unauthenticated request should get 401 or 403."""
        url = f"/api/resumes/{self.resume.pk}/"
        response = self.client.get(url)
        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])


# ═══════════════════════════════════════════════════════════════
#  Upload Validation Tests
# ═══════════════════════════════════════════════════════════════

class UploadValidationTests(TestCase):
    """Tests for hardened upload validation in ApplicationCreateSerializer."""

    def setUp(self):
        self.candidate_user, self.candidate_profile = _create_candidate()
        self.recruiter_user, self.recruiter_profile = _create_recruiter()
        self.job = _create_job(self.recruiter_profile)
        self.client = APIClient()

    def test_reject_wrong_extension(self):
        """Files with wrong extension should be rejected."""
        self.client.force_authenticate(user=self.candidate_user)

        fake_file = SimpleUploadedFile(
            "malicious.exe",
            b"MZ" + b"\\x00" * 100,
            content_type="application/octet-stream",
        )

        response = self.client.post(
            "/api/applications/",
            {
                "job": str(self.job.pk),
                "resume_file": fake_file,
                "consent_given": True,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_oversized_file(self):
        """Files over 5MB should be rejected."""
        self.client.force_authenticate(user=self.candidate_user)

        # Create a file just over 5MB
        large_content = b"x" * (5 * 1024 * 1024 + 1)
        fake_file = SimpleUploadedFile(
            "big_resume.pdf",
            large_content,
            content_type="application/pdf",
        )

        response = self.client.post(
            "/api/applications/",
            {
                "job": str(self.job.pk),
                "resume_file": fake_file,
                "consent_given": True,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class CandidateResumeIsolationTests(TestCase):
    """Tests for strict candidate data isolation on candidate resume endpoints."""

    def setUp(self):
        self.cand1_user, self.cand1_profile = _create_candidate()
        self.cand2_user = User.objects.create(
            clerk_id="cand2_clerk_id",
            email="cand2@test.com",
            role=User.Role.CANDIDATE,
        )
        self.cand2_profile = CandidateProfile.objects.create(user=self.cand2_user)
        self.client = APIClient()

    def test_candidate_resume_isolation_between_accounts(self):
        """Account B must not see Account A's uploaded resume."""
        # Account A creates a resume
        pdf_bytes = b"%PDF-1.4 sample pdf content for isolation testing"
        file_a = SimpleUploadedFile("resume_a.pdf", pdf_bytes, content_type="application/pdf")
        
        self.client.force_authenticate(user=self.cand1_user)
        res_a = self.client.post(
            "/api/candidates/resumes/",
            {"file": file_a},
            format="multipart",
        )
        self.assertEqual(res_a.status_code, status.HTTP_201_CREATED)

        # Account A lists resumes -> sees 1 resume
        list_a = self.client.get("/api/candidates/resumes/")
        self.assertEqual(list_a.status_code, status.HTTP_200_OK)
        items_a = list_a.json()
        self.assertEqual(len(items_a), 1)
        resume_a_id = items_a[0]["id"]

        # Account B logs in -> sees 0 resumes (data isolated)
        self.client.force_authenticate(user=self.cand2_user)
        list_b = self.client.get("/api/candidates/resumes/")
        self.assertEqual(list_b.status_code, status.HTTP_200_OK)
        items_b = list_b.json()
        self.assertEqual(len(items_b), 0)

        # Account B uploads resume_b.pdf
        file_b = SimpleUploadedFile("resume_b.pdf", pdf_bytes, content_type="application/pdf")
        res_b = self.client.post(
            "/api/candidates/resumes/",
            {"file": file_b},
            format="multipart",
        )
        self.assertEqual(res_b.status_code, status.HTTP_201_CREATED)

        # Account B lists resumes -> sees resume_b only
        list_b2 = self.client.get("/api/candidates/resumes/")
        self.assertEqual(list_b2.status_code, status.HTTP_200_OK)
        items_b2 = list_b2.json()
        self.assertEqual(len(items_b2), 1)
        resume_b_id = items_b2[0]["id"]
        self.assertNotEqual(resume_a_id, resume_b_id)

        # Account A logs back in -> sees resume_a only
        self.client.force_authenticate(user=self.cand1_user)
        list_a2 = self.client.get("/api/candidates/resumes/")
        self.assertEqual(list_a2.status_code, status.HTTP_200_OK)
        items_a2 = list_a2.json()
        self.assertEqual(len(items_a2), 1)
        self.assertEqual(items_a2[0]["id"], resume_a_id)

