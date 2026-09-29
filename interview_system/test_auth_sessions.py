from django.test import TestCase
from rest_framework.test import APIClient

from .models import User


class SessionIdentityTests(TestCase):
    def test_identity_requires_credential_proof(self):
        self.assertEqual(APIClient().get('/api/auth/login-role/').status_code, 401)

    def test_identity_returns_authoritative_role_and_identity_together(self):
        user = User.objects.create(clerk_id='session_identity', email='session@example.com', role='RECRUITER')
        client = APIClient()
        client.force_authenticate(user)
        response = client.get('/api/auth/login-role/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['role'], 'RECRUITER')
        self.assertEqual(response.data['clerk_id'], user.clerk_id)
        self.assertTrue(response.data['is_active'])
