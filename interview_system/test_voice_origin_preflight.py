from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .voice_views import VoiceInterviewMixin


@override_settings(INTERVIEW_ANALYSIS_WS_ALLOWED_ORIGINS=['http://localhost:50893'])
class VoiceOriginPreflightTests(SimpleTestCase):
    def request(self, origin, video=True):
        return SimpleNamespace(data={'video_required': video}, headers=(
            {} if origin is None else {'Origin': origin}))

    def test_unlisted_flutter_port_fails_before_call_or_database_work(self):
        with patch('interview_system.voice_views.transaction.atomic') as atomic:
            response = VoiceInterviewMixin().start_voice_session(
                self.request('http://localhost:54281'), pk='unused')
        self.assertEqual(response.status_code, 503)
        self.assertIn('INTERVIEW_ANALYSIS_WS_ALLOWED_ORIGINS', response.data['detail'])
        self.assertEqual(response['Cache-Control'], 'no-store')
        atomic.assert_not_called()

    def test_allowed_origin_and_non_browser_continue_normal_authorization(self):
        for origin, video in [('http://localhost:50893', True), (None, True),
                              ('http://localhost:54281', False)]:
            with self.subTest(origin=origin, video=video):
                with patch('interview_system.voice_views.transaction.atomic',
                           side_effect=RuntimeError('normal authorization boundary')):
                    with self.assertRaisesRegex(RuntimeError, 'normal authorization boundary'):
                        VoiceInterviewMixin().start_voice_session(self.request(origin, video), pk='unused')

    def test_malformed_origin_cannot_match_invalid_allowlist_entry(self):
        with override_settings(INTERVIEW_ANALYSIS_WS_ALLOWED_ORIGINS=['bad-origin']):
            response = VoiceInterviewMixin().start_voice_session(self.request('null'), pk='unused')
        self.assertEqual(response.status_code, 503)
