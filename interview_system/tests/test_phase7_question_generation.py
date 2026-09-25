"""Phase 7 Step 1 Gemini question generation and template fallback tests."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from interview_system.integrations.gemini_client import (
    QUESTION_CATEGORIES,
    generate_questions_for,
    get_template_questions,
)
from interview_system.models import TemplateQuestion


def generated_payload():
    return [
        {"text": "Tell me about a difficult collaboration.", "category": "BEHAVIORAL"},
        {"text": "How would you design a reliable service?", "category": "TECHNICAL"},
        {"text": "What would you do if requirements changed?", "category": "SITUATIONAL"},
    ]


@override_settings(
    GEMINI_API_KEY="question-test-key",
    GEMINI_QUESTION_MODEL_NAME="gemini-2.5-flash",
    GEMINI_QUESTION_TIMEOUT_MS=7000,
)
class QuestionGenerationTests(TestCase):
    def setUp(self):
        self.job = SimpleNamespace(
            title="Backend Engineer",
            description="Build reliable services.",
            requirements="Python and API design",
        )
        self.parsed_resume = SimpleNamespace(
            skills=["Python", "Django"],
            experience=[{"title": "Engineer", "description": "Built APIs"}],
        )

    @patch("google.genai.Client")
    def test_well_formed_gemini_response(self, client_class):
        client_class.return_value.models.generate_content.return_value.text = json.dumps(generated_payload())
        result = generate_questions_for(self.job, self.parsed_resume)
        self.assertEqual(result, [
            {**item, "source": "GENERATED"} for item in generated_payload()
        ])
        client_options = client_class.call_args.kwargs["http_options"]
        self.assertEqual(client_options.timeout, 7000)
        call = client_class.return_value.models.generate_content.call_args.kwargs
        self.assertEqual(call["model"], "gemini-2.5-flash")
        self.assertIn("Build reliable services", call["contents"])
        self.assertIn("Django", call["contents"])

    @patch("interview_system.integrations.gemini_client.time.sleep")
    @patch("google.genai.Client")
    def test_malformed_responses_retry_then_fall_back(self, client_class, sleep):
        malformed = (
            json.dumps([
                {"text": "Good text", "category": "BEHAVIORAL"},
                {"text": "Bad category", "category": "PERSONAL"},
            ]),
            json.dumps([
                {"category": "BEHAVIORAL"},
                {"text": "Technical", "category": "TECHNICAL"},
                {"text": "Situational", "category": "SITUATIONAL"},
            ]),
            "not-json",
        )
        for response_text in malformed:
            with self.subTest(response_text=response_text):
                generator = client_class.return_value.models.generate_content
                generator.reset_mock()
                sleep.reset_mock()
                generator.return_value.text = response_text
                result = generate_questions_for(self.job, self.parsed_resume)
                self._assert_template_set(result)
                self.assertEqual(generator.call_count, 3)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    @patch("interview_system.integrations.gemini_client.time.sleep")
    @patch("google.genai.Client")
    def test_timeout_or_api_error_falls_back(self, client_class, sleep):
        client_class.return_value.models.generate_content.side_effect = TimeoutError("Gemini timed out")
        result = generate_questions_for(self.job, self.parsed_resume)
        self._assert_template_set(result)
        self.assertEqual(client_class.return_value.models.generate_content.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_template_reader_returns_valid_seeded_questions(self):
        result = get_template_questions()
        self._assert_template_set(result)
        self.assertGreaterEqual(TemplateQuestion.objects.count(), 12)

    def test_empty_or_partial_table_still_returns_every_category(self):
        TemplateQuestion.objects.all().delete()
        TemplateQuestion.objects.create(text="A generic behavioral question?", category="BEHAVIORAL")
        result = get_template_questions()
        self._assert_template_set(result)
        self.assertIn("A generic behavioral question?", {item["text"] for item in result})

    def _assert_template_set(self, result):
        self.assertTrue(result)
        self.assertEqual({item["category"] for item in result}, QUESTION_CATEGORIES)
        for item in result:
            self.assertEqual(set(item), {"text", "category", "source"})
            self.assertTrue(item["text"].strip())
            self.assertEqual(item["source"], "TEMPLATE")
