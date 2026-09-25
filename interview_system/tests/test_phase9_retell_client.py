"""Offline Retell boundary tests. No database or external API is used."""

import json
import logging
import os
import traceback
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings

from interview_system.integrations.retell_client import (
    RetellUnavailableError,
    create_session,
)


@override_settings(RETELL_API_KEY="test-secret-key", RETELL_AGENT_ID="agent_test")
class RetellClientTests(SimpleTestCase):
    def setUp(self):
        self.interview = SimpleNamespace(pk="interview-123")
        self.questions = [
            SimpleNamespace(
                text="Explain a Python generator.", category="TECHNICAL",
                approved=True, interview_id="interview-123",
            ),
            SimpleNamespace(
                text="How do you handle feedback?", category="BEHAVIORAL",
                approved=True, interview_id="interview-123",
            ),
        ]
        self.post = self.enterContext(patch(
            "interview_system.integrations.retell_client.requests.post"
        ))
        self.response = Mock(status_code=201)
        self.response.json.return_value = {
            "call_id": "call_test123", "access_token": "private-join-token",
        }
        self.post.return_value = self.response

    def test_success_returns_id_and_sends_only_approved_context(self):
        result = create_session(self.interview, iter(self.questions))
        self.assertIs(type(result), str)
        self.assertEqual(result, "call_test123")
        self.post.assert_called_once()
        args, kwargs = self.post.call_args
        self.assertEqual(args, ("https://api.retellai.com/v3/create-web-call",))
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer test-secret-key"})
        self.assertEqual(kwargs["timeout"], (2.0, 5.0))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["json"]["agent_id"], "agent_test")
        self.assertEqual(kwargs["json"]["metadata"], {"interview_id": "interview-123"})
        context = json.loads(kwargs["json"]["retell_llm_dynamic_variables"]["interview_questions"])
        self.assertEqual(context, [
            {"text": q.text, "category": q.category} for q in self.questions
        ])
        self.response.close.assert_called_once()

    def _assert_safe_failure(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        handler = Capture()
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            try:
                create_session(self.interview, self.questions)
            except RetellUnavailableError as exc:
                formatted = "".join(traceback.format_exception(exc))
                message = str(exc)
                self.assertIsNone(exc.__cause__)
            else:
                self.fail("Expected a distinguishable Retell failure")
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        for secret in ("test-secret-key", "private-join-token"):
            self.assertNotIn(secret, message)
            self.assertNotIn(secret, formatted)
            self.assertNotIn(secret, "\n".join(records))

    def test_transport_failures_are_uniform_and_do_not_leak_secrets(self):
        for error in (requests.Timeout, requests.ConnectionError, requests.RequestException):
            with self.subTest(error=error):
                self.post.reset_mock()
                self.post.side_effect = error("test-secret-key private-join-token")
                self._assert_safe_failure()
                self.post.assert_called_once()  # No automatic duplicate creation.

    def test_http_failures_and_redirects_are_uniform_and_secret_safe(self):
        for status in (302, 400, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                self.response.status_code = status
                self.response.text = "test-secret-key private-join-token"
                self._assert_safe_failure()
        self.response.json.assert_not_called()
        self.assertEqual(self.response.close.call_count, 7)

    def test_invalid_json_is_secret_safe(self):
        self.response.json.side_effect = ValueError("test-secret-key private-join-token")
        self._assert_safe_failure()
        self.response.close.assert_called_once()

    def test_malformed_responses_raise_named_failure(self):
        for data in (None, [], {}, {"call_id": None}, {"call_id": 123},
                     {"call_id": ""}, {"call_id": "  "},
                     {"error": "test-secret-key private-join-token"}):
            with self.subTest(data=data):
                self.response.json.return_value = data
                self._assert_safe_failure()

    def test_missing_configuration_fails_before_network(self):
        for name in ("RETELL_API_KEY", "RETELL_AGENT_ID"):
            with self.subTest(name=name), override_settings(**{name: ""}):
                self._assert_safe_failure()
        self.post.assert_not_called()

    def test_explicit_empty_setting_does_not_use_environment_secret(self):
        with patch.dict(os.environ, {"RETELL_API_KEY": "environment-secret"}), override_settings(RETELL_API_KEY=""):
            self._assert_safe_failure()
        self.post.assert_not_called()

    def test_empty_or_invalid_question_sets_fail_before_network(self):
        for questions in ([], None, "text", {}, 3):
            with self.subTest(questions=questions):
                with self.assertRaises(RetellUnavailableError):
                    create_session(self.interview, questions)
        self.post.assert_not_called()

    def test_unapproved_foreign_and_invalid_questions_are_rejected(self):
        for field, value in (("approved", False), ("interview_id", "another"),
                             ("text", " "), ("category", "UNKNOWN")):
            question = vars(self.questions[0]).copy()
            question[field] = value
            with self.subTest(field=field), self.assertRaises(RetellUnavailableError):
                create_session(self.interview, [question])
        self.post.assert_not_called()

    def test_mapping_questions_are_supported(self):
        self.assertEqual(create_session(self.interview, [vars(q) for q in self.questions]), "call_test123")

    def test_missing_interview_id_is_rejected(self):
        with self.assertRaises(RetellUnavailableError):
            create_session(SimpleNamespace(pk=None), self.questions)
        self.post.assert_not_called()

    def test_programming_errors_are_not_disguised_as_unavailability(self):
        self.post.side_effect = TypeError("programming error")
        with self.assertRaises(TypeError):
            create_session(self.interview, self.questions)
