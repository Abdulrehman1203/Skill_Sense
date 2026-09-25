"""Webhook security and DB completion contracts; no Phase 8 consumer is present.

Completion tests exercise the hook intended for disconnect, not a fabricated
WebSocket. Actual Phase 8 disconnect integration remains a separate blocker.
"""
import hashlib
import hmac
import json
import time
from unittest.mock import patch

from django.db import OperationalError, connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from celery.exceptions import Retry
from kombu.exceptions import OperationalError as BrokerError
from rest_framework.test import APIClient

from interview_system.models import Interview, InterviewSession, Question
from interview_system.session_completion import mark_video_ended
from interview_system.tasks.interview_tasks import process_retell_transcript
from interview_system.tests import test_phase7_interview_scheduling as fixtures


@override_settings(RETELL_API_KEY='test-signing-secret')
class RetellWebhookTests(TransactionTestCase):
    _create_job = staticmethod(fixtures.Phase7InterviewTests._create_job)

    def setUp(self):
        fixtures.Phase7InterviewTests.setUp(self)
        self.interview = Interview.objects.create(application=self.application)
        Question.objects.create(interview=self.interview, approved=True, text='Explain Python.',
                                category='TECHNICAL', source='GENERATED')
        with patch('interview_system.integrations.retell_client.create_session', return_value='call_test'):
            result = self.client.post(reverse('interview_system:interview-start-voice-session', args=[self.interview.pk]))
        self.assertEqual(result.status_code, 201)
        self.session = InterviewSession.objects.get(pk=result.data['interview_session_id'])
        self.url = reverse('interview_system:retell-call-ended')
        self.client = APIClient()  # External caller has no JWT/session.
        self.payload = {'retell_session_id': 'call_test', 'transcript': 'Exact transcript.\n',
                        'ended_at': timezone.now().isoformat()}

    def signed_post(self, payload=None, raw=None, signature=None, timestamp=None):
        raw = raw if raw is not None else json.dumps(payload or self.payload).encode()
        if signature is None:
            timestamp = timestamp if timestamp is not None else int(time.time() * 1000)
            digest = hmac.new(b'test-signing-secret', raw + str(timestamp).encode(), hashlib.sha256).hexdigest()
            signature = f'v={timestamp},d={digest}'
        return self.client.post(self.url, raw, content_type='application/json',
                                HTTP_X_RETELL_SIGNATURE=signature, REMOTE_ADDR='192.0.2.7')

    def state(self):
        self.interview.refresh_from_db()
        self.session.refresh_from_db()
        return self.interview.status

    def deliver(self):
        with patch('interview_system.retell_webhook.process_retell_transcript.apply_async') as queue:
            self.assertEqual(self.signed_post().status_code, 202)
        payload = queue.call_args.kwargs['args'][0]
        return process_retell_transcript.run(payload)

    def test_valid_webhook_only_queues_then_task_persists(self):
        with patch('interview_system.retell_webhook.process_retell_transcript.apply_async') as queue:
            self.assertEqual(self.signed_post().status_code, 202)
            self.session.refresh_from_db()
            self.assertEqual(self.session.transcript, '')
            self.assertIsNone(self.session.audio_ended_at)
        self.assertFalse(queue.call_args.kwargs['retry'])
        process_retell_transcript.run(queue.call_args.kwargs['args'][0])
        self.assertEqual(self.state(), 'LIVE')
        self.assertEqual(self.session.transcript, self.payload['transcript'])
        self.assertIsNotNone(self.session.audio_ended_at)
        self.assertIsNone(self.session.ended_at)

    def test_invalid_signature_no_parsing_no_writes_no_enqueue(self):
        before_interview = Interview.objects.values().get(pk=self.interview.pk)
        before_session = InterviewSession.objects.values().get(pk=self.session.pk)
        for signature in ('', 'bad', 'v=1,d=' + '0' * 64):
            with self.subTest(signature=signature), patch('interview_system.retell_webhook.json.loads') as loads, \
                 patch('interview_system.retell_webhook.process_retell_transcript.apply_async') as queue, \
                 self.assertLogs('interview_system.retell_webhook', level='WARNING') as logs, \
                 CaptureQueriesContext(connection) as queries:
                response = self.signed_post(raw=b'{untrusted-secret:broken', signature=signature)
                self.assertEqual(response.status_code, 401)
                loads.assert_not_called()
                queue.assert_not_called()
                self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
            message = '\n'.join(logs.output)
            self.assertIn('192.0.2.7', message)
            self.assertIn('timestamp=', message)
            self.assertNotIn('untrusted-secret', message)
            self.assertNotIn('test-signing-secret', message)
        self.assertEqual(Interview.objects.values().get(pk=self.interview.pk), before_interview)
        self.assertEqual(InterviewSession.objects.values().get(pk=self.session.pk), before_session)

    def test_stale_future_and_tampered_signatures_rejected(self):
        with patch('interview_system.retell_webhook.process_retell_transcript.apply_async') as queue:
            for offset in (-301000, 301000):
                self.assertEqual(self.signed_post(timestamp=int(time.time()*1000)+offset).status_code, 401)
            self.assertEqual(self.signed_post(signature=f'v={int(time.time()*1000)},d=' + '0'*64).status_code, 401)
            queue.assert_not_called()

    def test_signed_bad_shape_and_json_are_400(self):
        with patch('interview_system.retell_webhook.process_retell_transcript.apply_async') as queue:
            for raw in (b'{', b'[]', b'{}', json.dumps({**self.payload, 'transcript': 123}).encode()):
                self.assertEqual(self.signed_post(raw=raw).status_code, 400)
            queue.assert_not_called()

    def test_provider_native_envelope_is_normalized_after_verification(self):
        with patch('interview_system.retell_webhook.process_retell_transcript.apply_async') as queue:
            result = self.signed_post(payload={'event': 'call_ended', 'call': {
                'call_id': 'call_test', 'transcript': 'Hi', 'end_timestamp': 1780000000000,
            }})
        self.assertEqual(result.status_code, 202)
        self.assertEqual(queue.call_args.kwargs['args'][0]['retell_session_id'], 'call_test')

    def test_video_hook_first_leaves_live_then_audio_finishes(self):
        mark_video_ended(self.session.pk)
        self.assertEqual(self.state(), 'LIVE')
        self.assertIsNotNone(self.session.ended_at)
        self.assertIsNone(self.session.audio_ended_at)
        self.assertEqual(self.deliver()['status'], 'processed')
        self.assertEqual(self.state(), 'DONE')

    def test_audio_first_leaves_live_then_video_hook_finishes(self):
        self.deliver()
        self.assertEqual(self.state(), 'LIVE')
        self.assertIsNone(self.session.ended_at)
        mark_video_ended(self.session.pk)
        self.assertEqual(self.state(), 'DONE')

    def test_duplicate_does_not_overwrite_or_reopen(self):
        self.deliver()
        mark_video_ended(self.session.pk)
        self.state()
        before = (self.session.audio_ended_at, self.session.ended_at)
        process_retell_transcript.run({**self.payload, 'transcript': 'changed'})
        self.assertEqual(self.state(), 'DONE')
        self.assertEqual(self.session.transcript, self.payload['transcript'])
        self.assertEqual((self.session.audio_ended_at, self.session.ended_at), before)

    def test_empty_transcript_still_marks_audio_complete(self):
        process_retell_transcript.run({**self.payload, 'transcript': ''})
        mark_video_ended(self.session.pk)
        self.assertEqual(self.state(), 'DONE')

    def test_unknown_session_is_logged_dropped_without_retry(self):
        with patch.object(process_retell_transcript, 'retry') as retry, \
             self.assertLogs('interview_system.tasks.interview_tasks', level='WARNING') as logs:
            result = process_retell_transcript.run({**self.payload, 'retell_session_id': 'missing'})
        self.assertEqual(result['status'], 'dropped')
        retry.assert_not_called()
        self.assertIn('missing', '\n'.join(logs.output))
        self.assertEqual(self.state(), 'LIVE')
        self.assertEqual(self.session.transcript, '')

    def test_queue_failure_returns_503_and_video_hook_remains_independent(self):
        with patch('interview_system.retell_webhook.process_retell_transcript.apply_async', side_effect=BrokerError('private')):
            self.assertEqual(self.signed_post().status_code, 503)
        mark_video_ended(self.session.pk)
        self.assertEqual(self.state(), 'LIVE')
        self.deliver()
        self.assertEqual(self.state(), 'DONE')

    def test_retry_policy_is_distinct_five_retries_under_twenty_second_budget(self):
        from interview_system.tasks.interview_tasks import _DELAYS
        self.assertEqual(process_retell_transcript.max_retries, 5)
        self.assertLess(sum(_DELAYS) + 6 * process_retell_transcript.time_limit, 20)
        with patch('interview_system.tasks.interview_tasks.Interview.objects.select_for_update', side_effect=OperationalError('private')), \
             patch.object(process_retell_transcript, 'retry', side_effect=Retry()) as retry:
            with self.assertRaises(Retry):
                process_retell_transcript.run(self.payload)
        self.assertEqual(retry.call_args.kwargs['countdown'], .25)
        self.assertIn('retell_deadline', retry.call_args.kwargs['headers'])

    def test_legacy_import_is_same_task(self):
        from interview_system.tasks.interviewing import process_retell_transcript as legacy
        self.assertIs(legacy, process_retell_transcript)
