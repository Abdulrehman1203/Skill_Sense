"""Phase 9 start-session gate, permissions, real commits, and rollback checks."""
from unittest.mock import patch

from django.db import IntegrityError, connection
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from interview_system.integrations.retell_client import RetellUnavailableError
from interview_system.models import Interview, InterviewSession, Question
from interview_system.serializers import StartVoiceSessionResponseSerializer
from interview_system.tests import test_phase7_interview_scheduling as scheduling


class StartVoiceSessionTests(TransactionTestCase):
    _create_job = staticmethod(scheduling.Phase7InterviewTests._create_job)

    def setUp(self):
        scheduling.Phase7InterviewTests.setUp(self)
        self.interview = Interview.objects.create(application=self.application)
        self.url = reverse('interview_system:interview-start-voice-session', args=[self.interview.pk])
        self.retell = self.enterContext(patch(
            'interview_system.integrations.retell_client.create_session',
            return_value='call_success',
        ))

    def question(self, approved=True, interview=None):
        return Question.objects.create(
            interview=interview or self.interview, text='Explain your approach.',
            category=Question.Category.TECHNICAL,
            source=Question.Source.GENERATED, approved=approved,
        )

    def assert_no_start(self):
        self.retell.assert_not_called()
        self.assertFalse(InterviewSession.objects.filter(interview=self.interview).exists())

    def test_status_gate_names_actual_status(self):
        self.question()
        for value in ('LIVE', 'DONE', 'UNKNOWN'):
            with self.subTest(value=value):
                Interview.objects.filter(pk=self.interview.pk).update(status=value)
                response = self.client.post(self.url)
                self.assertEqual(response.status_code, 400)
                self.assertIn(value, str(response.data['status']))
                self.assertIn('SCHEDULED', str(response.data['status']))
        self.assert_no_start()

    def test_zero_questions_rejected(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 400)
        self.assertIn('approved question', str(response.data['questions']))
        self.assert_no_start()

    def test_only_unapproved_questions_rejected(self):
        self.question(approved=False)
        other = Interview.objects.create(application=self.application)
        self.question(interview=other)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 400)
        self.assertIn('approved question', str(response.data['questions']))
        self.assert_no_start()

    def test_nonowner_is_403_before_precondition_checks(self):
        Interview.objects.filter(pk=self.interview.pk).update(status='DONE')
        self.client.force_authenticate(self.other_recruiter_user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 403)
        self.assertIn('own', str(response.data))
        self.assert_no_start()

    def test_candidate_is_forbidden(self):
        self.client.force_authenticate(self.candidate_user)
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assert_no_start()

    def test_success_commits_and_passes_only_approved_questions(self):
        approved = self.question()
        self.question(approved=False)
        other = Interview.objects.create(application=self.application)
        self.question(interview=other)

        def create(interview, questions):
            self.assertTrue(connection.in_atomic_block)
            self.assertTrue(InterviewSession.objects.filter(interview=interview).exists())
            self.assertEqual([q.pk for q in questions], [approved.pk])
            return 'call_success'

        self.retell.side_effect = create
        before = timezone.now()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(set(response.data), {'interview_session_id', 'retell_session_id'})
        self.assertEqual(response.data['retell_session_id'], 'call_success')
        session = InterviewSession.objects.get(pk=response.data['interview_session_id'])
        self.assertEqual(session.interview_id, self.interview.pk)
        self.assertGreaterEqual(session.started_at, before)
        self.interview.refresh_from_db()
        self.assertEqual(self.interview.status, 'LIVE')
        self.assertEqual(self.interview.retell_session_id, 'call_success')
        self.assertFalse(connection.in_atomic_block)  # Actually committed, no TestCase wrapper.
        self.retell.assert_called_once()

    def test_degradation_commits_null_and_logs_local_attempt_identifiers(self):
        self.question()
        Interview.objects.filter(pk=self.interview.pk).update(retell_session_id='stale_id')
        self.retell.side_effect = RetellUnavailableError('Retell session request failed.')
        with self.assertLogs('interview_system.views', level='WARNING') as logs:
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data['retell_session_id'])
        session = InterviewSession.objects.get(pk=response.data['interview_session_id'])
        self.assertIsNotNone(session.started_at)
        self.interview.refresh_from_db()
        self.assertEqual(self.interview.status, 'LIVE')
        self.assertIsNone(self.interview.retell_session_id)
        self.assertFalse(connection.in_atomic_block)
        self.assertEqual(len(logs.records), 1)
        message = logs.records[0].getMessage()
        for part in (str(self.interview.pk), str(session.pk), 'Retell session request failed.', 'proceeding without voice'):
            self.assertIn(part, message)
        self.assertIsNone(logs.records[0].exc_info)

    def test_repeat_start_is_rejected_without_second_retell_call(self):
        self.question()
        self.assertEqual(self.client.post(self.url).status_code, 201)
        self.assertEqual(self.client.post(self.url).status_code, 400)
        self.assertEqual(InterviewSession.objects.filter(interview=self.interview).count(), 1)
        self.retell.assert_called_once()

    def test_database_failure_rolls_back_local_session_and_status(self):
        self.question()
        with patch.object(Interview, 'save', side_effect=IntegrityError('simulated write failure')):
            with self.assertRaises(IntegrityError):
                self.client.post(self.url)
        self.assertFalse(InterviewSession.objects.filter(interview=self.interview).exists())
        self.interview.refresh_from_db()
        self.assertEqual(self.interview.status, 'SCHEDULED')
        self.assertIsNone(self.interview.retell_session_id)

    def test_unexpected_error_rolls_back_instead_of_degrading(self):
        self.question()
        self.retell.side_effect = TypeError('programming error')
        with self.assertRaises(TypeError):
            self.client.post(self.url)
        self.assertFalse(InterviewSession.objects.filter(interview=self.interview).exists())
        self.interview.refresh_from_db()
        self.assertEqual(self.interview.status, 'SCHEDULED')

    def test_existing_phase8_session_is_not_overwritten(self):
        self.question()
        session = InterviewSession.objects.create(interview=self.interview, transcript='existing')
        self.assertEqual(self.client.post(self.url).status_code, 400)
        session.refresh_from_db()
        self.assertEqual(session.transcript, 'existing')
        self.retell.assert_not_called()

    def test_response_serializer_explicitly_allows_null_retell_id(self):
        field = StartVoiceSessionResponseSerializer().fields['retell_session_id']
        self.assertTrue(field.allow_null)
        self.assertIn('null', field.help_text)
