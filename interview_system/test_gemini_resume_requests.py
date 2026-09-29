"""Resume request configuration and diagnostics, without live API calls."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from google.genai import types

from interview_system.integrations.gemini_client import GeminiParseError, parse_resume_text


@override_settings(
    GEMINI_API_KEY="test-key",
    GEMINI_MODEL_NAME="gemini-3.5-flash-lite",
    GEMINI_REQUEST_TIMEOUT_MS=60000,
    GEMINI_PARSE_MAX_OUTPUT_TOKENS=4096,
)
class GeminiResumeRequestTests(SimpleTestCase):
    def setUp(self):
        self.client_patch = patch("google.genai.Client")
        self.client_class = self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.generate = self.client_class.return_value.models.generate_content
        self.data = {
            "skills": ["Python"],
            "education": [{"degree": "BS", "institution": "University", "year": None}],
            "experience": [],
            "certifications": [],
        }
        self.generate.return_value = types.GenerateContentResponse(
            candidates=[types.Candidate(
                content=types.Content(parts=[types.Part(text=json.dumps(self.data))]),
                finish_reason=types.FinishReason.STOP,
            )],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=1250, candidates_token_count=250,
                thoughts_token_count=10, total_token_count=1510,
            ),
        )

    def test_schema_and_low_latency_settings_preserve_parsed_fields(self):
        self.assertEqual(parse_resume_text("Resume text"), self.data)
        config = self.generate.call_args.kwargs["config"]
        self.assertEqual(config.max_output_tokens, 4096)
        self.assertEqual(config.thinking_config.thinking_level, types.ThinkingLevel.MINIMAL)
        self.assertEqual(config.temperature, 1.0)
        schema = config.response_json_schema
        self.assertEqual(set(schema["required"]), set(self.data))
        self.assertFalse(schema["additionalProperties"])
        year = schema["properties"]["education"]["items"]["properties"]["year"]
        self.assertEqual(year["type"], ["integer", "null"])
        self.assertEqual(self.client_class.call_args.kwargs["http_options"].retry_options.attempts, 1)
        self.generate.assert_called_once()

    @override_settings(GEMINI_PARSE_MAX_OUTPUT_TOKENS=8192)
    def test_output_token_budget_is_configurable(self):
        parse_resume_text("Resume text")
        self.assertEqual(self.generate.call_args.kwargs["config"].max_output_tokens, 8192)

    @override_settings(GEMINI_MODEL_NAME="gemini-2.5-flash-lite")
    def test_older_model_does_not_receive_gemini_3_thinking_level(self):
        parse_resume_text("Resume text")
        config = self.generate.call_args.kwargs["config"]
        self.assertIsNone(config.thinking_config)
        self.assertEqual(config.temperature, 0.0)

    def test_duration_and_token_logs_exclude_resume_content(self):
        with patch("interview_system.integrations.gemini_client.time.monotonic", side_effect=[100, 102.5]), \
                self.assertLogs("interview_system.integrations.gemini_client", level="INFO") as logs:
            parse_resume_text("Private resume content")
        output = "\n".join(logs.output)
        for expected in ("duration_s=2.500", "prompt_tokens=1250", "output_tokens=250",
                         "thinking_tokens=10", "total_tokens=1510", "finish_reason="):
            self.assertIn(expected, output)
        self.assertNotIn("Private resume content", output)

    @override_settings(GEMINI_MODEL_NAME="gemini-3.1-pro-preview")
    def test_pro_override_does_not_receive_unsupported_minimal_level(self):
        parse_resume_text("Resume text")
        self.assertIsNone(self.generate.call_args.kwargs["config"].thinking_config)

    def test_response_without_usage_metadata_still_parses(self):
        self.generate.return_value = SimpleNamespace(text=json.dumps(self.data))
        self.assertEqual(parse_resume_text("Resume text"), self.data)

    def test_timeout_is_logged_and_wrapped_without_internal_retry(self):
        self.generate.side_effect = TimeoutError("deadline exceeded")
        with patch("interview_system.integrations.gemini_client.time.monotonic", side_effect=[100, 160]), \
                self.assertLogs("interview_system.integrations.gemini_client", level="WARNING") as logs:
            with self.assertRaises(GeminiParseError) as caught:
                parse_resume_text("Private resume content")
        self.assertIsInstance(caught.exception.__cause__, TimeoutError)
        self.assertIn("duration_s=60.000", logs.output[0])
        self.assertNotIn("Private resume content", logs.output[0])
        self.generate.assert_called_once()

    def test_token_limited_response_is_rejected_even_if_json_is_valid(self):
        self.generate.return_value.candidates[0].finish_reason = types.FinishReason.MAX_TOKENS
        with self.assertRaisesMessage(GeminiParseError, "GEMINI_PARSE_MAX_OUTPUT_TOKENS"):
            parse_resume_text("Resume text")

    def test_invalid_json_does_not_expose_response_content_in_error(self):
        self.generate.return_value = SimpleNamespace(text="Private candidate details")
        with self.assertRaises(GeminiParseError) as caught:
            parse_resume_text("Resume text")
        self.assertNotIn("Private candidate details", str(caught.exception))
