"""Phase 6 Step 3 scoring task contracts and governance checks."""

from unittest.mock import patch

from django.test import TestCase

from interview_system.models import (
    Application, BehavioralAnalysis, CandidateProfile, CandidateScore, Interview,
    InterviewSession, Job, ParsedResume, RecruiterProfile, Resume, ScoringRubric, User,
)
from interview_system.tasks.analysis import generate_candidate_score as chain_scoring_task
from interview_system.tasks.scoring_tasks import (
    ScoringConfigurationError, ScoringInputNotReady, generate_candidate_score,
)


class ScoringTaskTests(TestCase):
    def setUp(self):
        recruiter = User.objects.create(clerk_id="score_recruiter", email="score-recruiter@example.test", role=User.Role.RECRUITER)
        candidate = User.objects.create(clerk_id="score_candidate", email="score-candidate@example.test", role=User.Role.CANDIDATE)
        recruiter_profile = RecruiterProfile.objects.create(user=recruiter)
        candidate_profile = CandidateProfile.objects.create(user=candidate)
        job = Job.objects.create(
            recruiter=recruiter_profile, title="Engineer", description="Python",
            requirements="Python", skills_required=["Python"], location="Remote",
            job_type=Job.JobType.REMOTE, experience_level=Job.ExperienceLevel.ENTRY,
            status=Job.Status.ACTIVE,
        )
        self.resume = Resume.objects.create(
            candidate=candidate_profile, file="resumes/scoring.pdf", status=Resume.Status.PARSED,
        )
        self.application = Application.objects.create(
            candidate=candidate_profile, job=job, resume=self.resume,
        )
        self.parsed = ParsedResume.objects.create(resume=self.resume, raw_text="Python engineer", match_score=82.0)
        self.rubric = ScoringRubric.objects.create(
            name="Default", weight_match=0.5, weight_interview=0.3,
            weight_behavioral=0.2, active=True,
        )

    @patch("interview_system.tasks.notifications.send_notification.delay")
    def test_match_only_happy_path_persists_missing_signal_explanation(self, notification):
        result = generate_candidate_score.run(application_id=str(self.application.pk))
        saved = CandidateScore.objects.get(application=self.application)
        self.assertEqual(result["final_score"], 41.0)
        self.assertEqual(saved.final_score, 41.0)
        self.assertEqual(saved.breakdown, {"match": 82.0, "interview": None, "behavioral": None})
        self.assertIn("interview signal unavailable — interview not yet conducted", saved.explanation)
        self.assertIn("behavioral signal unavailable — vision pipeline did not run", saved.explanation)
        self.assertIs(generate_candidate_score, chain_scoring_task)
        self.assertEqual(generate_candidate_score.name, "interview_system.tasks.analysis.generate_candidate_score")
        notification.assert_called_once_with(str(self.application.candidate_id), event={"type": "score_ready"})

    @patch("interview_system.tasks.notifications.send_notification.delay")
    def test_application_status_unchanged_for_low_and_high_scores(self, notification):
        self.rubric.weight_match = 1.0
        self.rubric.weight_interview = 0.0
        self.rubric.weight_behavioral = 0.0
        self.rubric.save(update_fields=["weight_match", "weight_interview", "weight_behavioral"])
        self.application.status = Application.Status.SCREENED
        self.application.save(update_fields=["status"])
        for match_value in (2.0, 98.0):
            with self.subTest(match_value=match_value):
                self.parsed.match_score = match_value
                self.parsed.save(update_fields=["match_score"])
                result = generate_candidate_score.run(application_id=str(self.application.pk))
                self.assertEqual(result["final_score"], match_value)
                self.application.refresh_from_db()
                self.assertEqual(self.application.status, Application.Status.SCREENED)
        self.assertEqual(CandidateScore.objects.filter(application=self.application).count(), 1)
        self.assertEqual(notification.call_count, 2)

    def test_notification_is_dispatched_after_score_persistence(self):
        def check_persisted(*args, **kwargs):
            self.assertTrue(CandidateScore.objects.filter(application=self.application).exists())

        with patch("interview_system.tasks.notifications.send_notification.delay", side_effect=check_persisted) as notification:
            generate_candidate_score.run(application_id=str(self.application.pk))
        notification.assert_called_once_with(str(self.application.candidate_id), event={"type": "score_ready"})

    @patch("interview_system.tasks.notifications.send_notification.delay")
    def test_no_active_rubric_fails_loudly(self, notification):
        self.rubric.active = False
        self.rubric.save(update_fields=["active"])
        with self.assertRaisesRegex(ScoringConfigurationError, "Exactly one active scoring rubric"):
            generate_candidate_score.run(application_id=str(self.application.pk))
        self.assertFalse(CandidateScore.objects.filter(application=self.application).exists())
        notification.assert_not_called()

    @patch("interview_system.tasks.notifications.send_notification.delay")
    def test_behavioral_data_is_used_when_available(self, notification):
        interview = Interview.objects.create(application=self.application)
        session = InterviewSession.objects.create(interview=interview, transcript="Stub transcript")
        BehavioralAnalysis.objects.create(
            session=session, attention_pct=80.0,
            integrity_flags=[{"type": "look_away"}, {"type": "multiple_faces"}],
        )
        result = generate_candidate_score.run(application_id=str(self.application.pk))
        self.assertEqual(result["breakdown"], {"match": 82.0, "interview": None, "behavioral": 60.0})
        self.assertEqual(result["final_score"], 53.0)
        self.assertIn("interview signal unavailable", result["explanation"])
        self.assertNotIn("behavioral signal unavailable", result["explanation"])

    @patch("interview_system.tasks.notifications.send_notification.delay")
    def test_missing_match_is_allowed_only_after_resume_failure(self, notification):
        self.parsed.match_score = None
        self.parsed.save(update_fields=["match_score"])
        with self.assertRaises(ScoringInputNotReady):
            generate_candidate_score.run(application_id=str(self.application.pk))
        self.resume.status = Resume.Status.FAILED
        self.resume.save(update_fields=["status"])
        result = generate_candidate_score.run(application_id=str(self.application.pk))
        self.assertEqual(result["final_score"], 0.0)
        self.assertEqual(result["breakdown"], {"match": None, "interview": None, "behavioral": None})
        self.assertIn("match signal unavailable — resume parsing failed entirely", result["explanation"])

    @patch("interview_system.tasks.notifications.send_notification.delay")
    def test_failed_resume_does_not_reuse_a_stale_match_score(self, notification):
        self.resume.status = Resume.Status.FAILED
        self.resume.save(update_fields=["status"])
        result = generate_candidate_score.run(application_id=str(self.application.pk))
        self.assertEqual(result["final_score"], 0.0)
        self.assertIsNone(result["breakdown"]["match"])
