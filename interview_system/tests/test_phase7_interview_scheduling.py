"""Phase 7 Step 2 interview scheduling and question persistence contracts."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from interview_system.models import (
    Application,
    CandidateProfile,
    Interview,
    Job,
    ParsedResume,
    Question,
    RecruiterProfile,
    Resume,
    User,
)
from interview_system.tasks.interviewing import generate_questions


class Phase7InterviewTests(TestCase):
    def setUp(self):
        self.recruiter_user = User.objects.create(
            clerk_id="phase7_recruiter",
            email="phase7-recruiter@example.test",
            role=User.Role.RECRUITER,
        )
        self.other_recruiter_user = User.objects.create(
            clerk_id="phase7_other_recruiter",
            email="phase7-other-recruiter@example.test",
            role=User.Role.RECRUITER,
        )
        self.candidate_user = User.objects.create(
            clerk_id="phase7_candidate",
            email="phase7-candidate@example.test",
            role=User.Role.CANDIDATE,
        )
        self.recruiter = RecruiterProfile.objects.create(user=self.recruiter_user)
        self.other_recruiter = RecruiterProfile.objects.create(
            user=self.other_recruiter_user
        )
        self.candidate = CandidateProfile.objects.create(user=self.candidate_user)
        self.job = self._create_job(self.recruiter, "Platform Engineer")
        self.other_job = self._create_job(self.other_recruiter, "Data Engineer")
        self.resume = Resume.objects.create(
            candidate=self.candidate,
            file="resumes/phase7.pdf",
            status=Resume.Status.PARSED,
        )
        self.parsed_resume = ParsedResume.objects.create(
            resume=self.resume,
            skills=["Python", "Django"],
            experience=[{"title": "Engineer", "years": 3}],
            raw_text="Python and Django engineer",
            match_score=84.0,
        )
        self.application = Application.objects.create(
            candidate=self.candidate,
            job=self.job,
            resume=self.resume,
            status=Application.Status.SCREENED,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.recruiter_user)
        self.url = reverse("interview_system:interview-list")

    @staticmethod
    def _create_job(recruiter, title):
        return Job.objects.create(
            recruiter=recruiter,
            title=title,
            description="Build reliable systems.",
            requirements="Python and communication",
            skills_required=["Python"],
            location="Remote",
            job_type=Job.JobType.REMOTE,
            experience_level=Job.ExperienceLevel.ENTRY,
            status=Job.Status.ACTIVE,
        )

    def _payload(self, application=None, scheduled_at=None):
        return {
            "application": str((application or self.application).pk),
            "scheduled_at": (
                scheduled_at or timezone.now() + timedelta(days=2)
            ).isoformat(),
        }

    @patch("interview_system.tasks.interviewing.generate_questions.delay")
    def test_schedule_happy_path_commits_then_dispatches(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, self._payload(), format="json")

        self.assertEqual(response.status_code, 201)
        interview = Interview.objects.get(pk=response.data["id"])
        self.assertEqual(interview.status, Interview.Status.SCHEDULED)
        self.assertEqual(response.data["questions"], [])
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, Application.Status.INTERVIEWED)
        delay.assert_called_once_with(str(interview.pk))

    def test_non_screened_application_is_rejected_for_applied_and_interviewed(self):
        for starting_status in (
            Application.Status.APPLIED,
            Application.Status.INTERVIEWED,
        ):
            with self.subTest(starting_status=starting_status):
                self.application.status = starting_status
                self.application.save(update_fields=["status"])
                response = self.client.post(self.url, self._payload(), format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn("must be in SCREENED status", str(response.data))
        self.assertFalse(Interview.objects.exists())

    def test_present_or_past_schedule_is_rejected(self):
        response = self.client.post(
            self.url,
            self._payload(scheduled_at=timezone.now() - timedelta(seconds=1)),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("future time", str(response.data))
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, Application.Status.SCREENED)

    def test_other_recruiters_application_is_forbidden(self):
        foreign_application = Application.objects.create(
            candidate=self.candidate,
            job=self.other_job,
            resume=self.resume,
            status=Application.Status.SCREENED,
        )
        response = self.client.post(
            self.url, self._payload(application=foreign_application), format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("do not have permission", str(response.data))
        foreign_application.refresh_from_db()
        self.assertEqual(foreign_application.status, Application.Status.SCREENED)

    def test_generic_advance_rejects_screened_to_interviewed(self):
        response = self.client.patch(
            reverse(
                "interview_system:application-advance", args=[self.application.pk]
            ),
            {"status": Application.Status.INTERVIEWED},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("POST /api/interviews/", str(response.data))
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, Application.Status.SCREENED)

    @patch("interview_system.integrations.gemini_client.generate_questions_for")
    def test_task_persists_generated_questions_unapproved(self, generate):
        self.assertEqual(
            generate_questions.name,
            "interview_system.tasks.interviewing.generate_questions",
        )
        interview = Interview.objects.create(
            application=self.application,
            status=Interview.Status.SCHEDULED,
            scheduled_at=timezone.now() + timedelta(days=1),
        )
        generate.return_value = [
            {
                "text": "Tell me about a difficult collaboration.",
                "category": Question.Category.BEHAVIORAL,
                "source": Question.Source.GENERATED,
            },
            {
                "text": "How would you design a reliable API?",
                "category": Question.Category.TECHNICAL,
                "source": Question.Source.GENERATED,
            },
            {
                "text": "What would you do if requirements changed?",
                "category": Question.Category.SITUATIONAL,
                "source": Question.Source.GENERATED,
            },
        ]

        result = generate_questions.run(interview_id=str(interview.pk))

        self.assertEqual(len(result), 3)
        saved = list(Question.objects.filter(interview=interview))
        self.assertEqual(len(saved), 3)
        self.assertEqual({question.source for question in saved}, {"GENERATED"})
        self.assertTrue(all(not question.approved for question in saved))
        generate.assert_called_once_with(self.job, self.parsed_resume)

    @patch("interview_system.integrations.gemini_client.get_template_questions")
    @patch("interview_system.integrations.gemini_client.generate_questions_for")
    def test_task_falls_back_to_nonempty_template_questions(
        self, generate, get_templates
    ):
        interview = Interview.objects.create(
            application=self.application,
            status=Interview.Status.SCHEDULED,
            scheduled_at=timezone.now() + timedelta(days=1),
        )
        generate.side_effect = TimeoutError("unexpected integration failure")
        get_templates.return_value = [
            {
                "text": "Describe how you prioritize competing responsibilities.",
                "category": Question.Category.BEHAVIORAL,
                "source": Question.Source.TEMPLATE,
            },
            {
                "text": "How do you diagnose an unfamiliar technical problem?",
                "category": Question.Category.TECHNICAL,
                "source": Question.Source.TEMPLATE,
            },
            {
                "text": "What would you do if a deadline were at risk?",
                "category": Question.Category.SITUATIONAL,
                "source": Question.Source.TEMPLATE,
            },
        ]

        result = generate_questions.run(interview_id=str(interview.pk))

        self.assertTrue(result)
        saved = list(Question.objects.filter(interview=interview))
        self.assertTrue(saved)
        self.assertEqual({question.source for question in saved}, {"TEMPLATE"})
        self.assertTrue(all(not question.approved for question in saved))
        get_templates.assert_called_once_with()
