import json
from datetime import datetime, timezone
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIClient

from .models import User
from .authentication import ClerkJWTAuthentication


class TokenClockSkewTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwks = {'keys': [{**public_jwk, 'kid': 'test-clock'}]}

    def verify(self, **claims):
        now = 1800000000
        payload = {'sub': 'user_clock', 'iat': now, 'nbf': now, 'exp': now + 60}
        payload.update({key: now + offset for key, offset in claims.items()})
        token = jwt.encode(payload, self.key, algorithm='RS256', headers={'kid': 'test-clock'})
        with patch('jwt.api_jwt.datetime') as clock:
            clock.now.return_value = datetime.fromtimestamp(now, timezone.utc)
            return ClerkJWTAuthentication()._verify_token(token, self.jwks)

    def test_small_issuer_clock_skew_is_accepted(self):
        self.assertEqual(self.verify(iat=4, nbf=4)['sub'], 'user_clock')

    def test_future_tokens_beyond_tolerance_are_rejected(self):
        for claim in ('iat', 'nbf'):
            with self.subTest(claim=claim), self.assertRaises(AuthenticationFailed):
                self.verify(**{claim: 6})

    def test_expired_tokens_are_rejected_in_development_and_production(self):
        for debug in (True, False):
            with self.subTest(debug=debug), override_settings(DEBUG=debug):
                with self.assertRaisesMessage(AuthenticationFailed, 'Token has expired'):
                    self.verify(iat=-120, nbf=-120, exp=-6)

    def test_invalid_signature_is_still_rejected(self):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode({'sub': 'forged'}, other_key, algorithm='RS256', headers={'kid': 'test-clock'})
        with self.assertRaises(AuthenticationFailed):
            ClerkJWTAuthentication()._verify_token(token, self.jwks)


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
