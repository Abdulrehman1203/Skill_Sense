"""Question review contracts and combined Phase 7 acceptance checks."""
import json
import uuid
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse

from interview_system.models import Application, Interview, Question
from interview_system.tasks.interviewing import generate_questions
from interview_system.tests import test_phase7_interview_scheduling as scheduling_tests


# Reuse setup without inheriting the Step 2 tests a second time.
from rest_framework.test import APITestCase


class QuestionReviewTests(APITestCase):
    setUp = scheduling_tests.Phase7InterviewTests.setUp
    _create_job = staticmethod(scheduling_tests.Phase7InterviewTests._create_job)
    _payload = scheduling_tests.Phase7InterviewTests._payload

    def prepare_questions(self):
        self.interview = Interview.objects.create(application=self.application)
        self.questions = [Question.objects.create(
            interview=self.interview, text=f"Question {i}?", category=category,
            source="GENERATED" if i % 2 else "TEMPLATE",
        ) for i, category in enumerate(Question.Category.values)]
        self.review_url = reverse("interview_system:interview-questions", args=[self.interview.pk])

    def snapshot(self, question):
        return json.dumps(Question.objects.filter(pk=question.pk).values().get(),
                          sort_keys=True, default=str).encode()

    def test_get_exact_shape_all_sources_and_empty_pending_set(self):
        self.prepare_questions()
        response = self.client.get(self.review_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 3)
        self.assertEqual({row["source"] for row in response.data}, {"GENERATED", "TEMPLATE"})
        for row in response.data:
            self.assertEqual(set(row), {"id", "text", "category", "source", "approved"})
        self.interview.questions.all().delete()
        self.assertEqual(self.client.get(self.review_url).data, [])

    def test_ownership_matches_application_get_and_missing_is_404(self):
        self.prepare_questions()
        self.assertEqual(self.client.get("/api/interviews/not-a-uuid/questions/").status_code, 404)
        self.client.force_authenticate(self.other_recruiter_user)
        app_response = self.client.get(reverse("interview_system:application-detail", args=[self.application.pk]))
        self.assertEqual(app_response.status_code, 404)
        for method in (self.client.get, self.client.patch):
            self.assertEqual(method(self.review_url).status_code, 404)
            missing = reverse("interview_system:interview-questions", args=[uuid.uuid4()])
            self.assertEqual(method(missing).status_code, 404)
        self.client.force_authenticate(self.candidate_user)
        self.assertEqual(self.client.get(self.review_url).status_code, 403)
        self.assertEqual(self.client.patch(self.review_url).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.review_url).status_code, 401)

    def test_targeted_edit_and_approval_preserve_unmentioned_row_byte_for_byte(self):
        self.prepare_questions()
        before = self.snapshot(self.questions[2])
        response = self.client.patch(self.review_url, {"questions": [
            {"id": str(self.questions[0].pk), "text": "Revised wording?", "approved": True},
            {"id": str(self.questions[1].pk), "approved": True},
        ]}, format="json")
        self.assertEqual(response.status_code, 200)
        self.questions[0].refresh_from_db()
        self.assertEqual(self.questions[0].text, "Revised wording?")
        self.assertTrue(self.questions[0].approved)
        self.assertEqual(before, self.snapshot(self.questions[2]))

    def test_invalid_id_rolls_back_entire_request(self):
        self.prepare_questions()
        foreign = Question.objects.create(
            interview=Interview.objects.create(application=self.application),
            text="Other interview", category="TECHNICAL", source="TEMPLATE",
        )
        before = [self.snapshot(q) for q in self.questions + [foreign]]
        for bad_id in (foreign.pk, uuid.uuid4()):
            response = self.client.patch(self.review_url, {"questions": [
                {"id": str(self.questions[0].pk), "text": "Must not persist", "approved": True},
                {"id": str(bad_id), "approved": True},
            ]}, format="json")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(before, [self.snapshot(q) for q in self.questions + [foreign]])

    def test_text_and_approval_independent_and_can_unapprove_all(self):
        self.prepare_questions()
        q = self.questions[0]
        for changes, expected_text, expected_approved in (
            ({"approved": True}, q.text, True),
            ({"text": "Edited alone?"}, "Edited alone?", True),
            ({"approved": False}, "Edited alone?", False),
        ):
            response = self.client.patch(self.review_url, {"questions": [
                {"id": str(q.pk), **changes},
            ]}, format="json")
            self.assertEqual(response.status_code, 200)
            q.refresh_from_db()
            self.assertEqual((q.text, q.approved), (expected_text, expected_approved))
        self.assertFalse(self.interview.questions.filter(approved=True).exists())

    def test_invalid_patch_shapes_and_duplicate_ids(self):
        self.prepare_questions()
        qid = str(self.questions[0].pk)
        for payload in ({}, {"questions": []}, {"questions": [{"id": qid}]},
                        {"questions": [{"id": qid, "text": " "}]},
                        {"questions": [{"id": qid, "approved": True, "source": "TEMPLATE"}]},
                        {"questions": [{"id": qid, "approved": True}] * 2}):
            self.assertEqual(self.client.patch(self.review_url, payload, format="json").status_code, 400)

    @override_settings(GEMINI_API_KEY="test-key", GEMINI_QUESTION_TIMEOUT_MS=60000)
    def test_combined_scheduling_generation_review_and_budget(self):
        for failed in (False, True):
            with self.subTest(fallback=failed):
                Application.objects.filter(pk=self.application.pk).update(status="SCREENED")
                elapsed = [0]
                def response(**kwargs):
                    if failed:
                        elapsed[0] += 7
                        raise TimeoutError("simulated request timeout")
                    from types import SimpleNamespace
                    return SimpleNamespace(text=json.dumps([
                        {"text": f"A {category} question?", "category": category}
                        for category in Question.Category.values
                    ]))
                with patch("google.genai.Client") as sdk, \
                     patch("interview_system.integrations.gemini_client.time.sleep",
                           side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)), \
                     patch("interview_system.tasks.interviewing.generate_questions.delay",
                           side_effect=lambda interview_id: generate_questions.run(interview_id)):
                    sdk.return_value.models.generate_content.side_effect = response
                    with self.captureOnCommitCallbacks(execute=True):
                        scheduled = self.client.post(self.url, self._payload(), format="json")
                    self.assertEqual(scheduled.status_code, 201)
                    self.assertEqual(scheduled.data["questions"], [])
                    options = sdk.call_args.kwargs["http_options"]
                    self.assertEqual(options.timeout, 7000)
                    self.assertEqual(options.retry_options.attempts, 1)
                    self.assertEqual(sdk.call_count, 3 if failed else 1)
                    self.assertEqual(elapsed[0], 24 if failed else 0)
                self.application.refresh_from_db()
                self.assertEqual(self.application.status, "INTERVIEWED")
                review_url = reverse("interview_system:interview-questions", args=[scheduled.data["id"]])
                questions = self.client.get(review_url).data
                self.assertEqual({q["category"] for q in questions}, set(Question.Category.values))
                self.assertEqual({q["source"] for q in questions}, {"TEMPLATE" if failed else "GENERATED"})
                self.assertTrue(all(not q["approved"] for q in questions))
                edited = self.client.patch(review_url, {"questions": [
                    {"id": questions[0]["id"], "text": "Recruiter revision?", "approved": True},
                ]}, format="json")
                self.assertEqual(edited.status_code, 200)
                with patch("interview_system.integrations.gemini_client.generate_questions_for") as generator:
                    generate_questions.run(scheduled.data["id"])
                    generator.assert_not_called()
                self.assertEqual(self.client.get(review_url).data, edited.data)

    @patch("interview_system.integrations.gemini_client.generate_questions_for")
    def test_missing_parsed_resume_still_falls_back(self, generator):
        self.parsed_resume.delete()
        self.prepare_questions()
        self.interview.questions.all().delete()
        generator.side_effect = RuntimeError("generation unavailable")
        generate_questions.run(str(self.interview.pk))
        self.assertTrue(self.interview.questions.exists())
        self.assertIsNone(generator.call_args.args[1])
