"""
Clerk JWT Authentication backend for Django REST Framework.

Verifies RS256-signed JWT access tokens issued by Clerk against Clerk's
JSON Web Key Set (JWKS). Resolves the authenticated user via their `clerk_id`
(the token's `sub` claim).
"""

from __future__ import annotations

import logging
from urllib.parse import quote
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.views.decorators.debug import sensitive_variables
import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError, PyJWKSetError
import requests
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed, ValidationError

from .models import User

logger = logging.getLogger(__name__)

JWKS_CACHE_KEY = "clerk:jwks"
JWKS_CACHE_TTL = 3600  # 1 hour TTL


class ClerkJWTAuthentication(BaseAuthentication):
    """
    DRF Authentication class for Clerk JWT verification.

    1. Extracts 'Bearer <token>' from the Authorization header.
    2. Fetches JWKS from Redis cache (key: "clerk:jwks", 1h TTL). On cache miss,
       fetches fresh keys from CLERK_JWKS_URL and populates cache.
    3. Verifies the RS256 signature and expiration via PyJWT.
    4. Resolves local User by clerk_id (JWT 'sub' claim).
    5. Resolves a missing local user from Clerk's server-side user record.
    6. Rejects if user is_active=False.
    """

    def authenticate_header(self, request: Any) -> str:
        return "Bearer"

    @sensitive_variables('auth_header', 'raw_token')
    def authenticate(self, request: Any) -> tuple[User, dict[str, Any]] | None:
        auth_header = get_authorization_header(request).split()

        if not auth_header or auth_header[0].lower() != b"bearer":
            return None

        if len(auth_header) == 1:
            raise AuthenticationFailed("Invalid bearer header. No token provided.")
        elif len(auth_header) > 2:
            raise AuthenticationFailed("Invalid bearer header. Token contains spaces.")

        try:
            raw_token = auth_header[1].decode("ascii")
        except UnicodeDecodeError:
            raise AuthenticationFailed("Invalid bearer header.") from None
        return self.authenticate_credentials(raw_token)

    @sensitive_variables('raw_token', 'payload')
    def authenticate_credentials(self, raw_token: str) -> tuple[User, dict[str, Any]]:
        jwks_data = self._get_jwks()
        if not jwks_data:
            logger.error("Clerk JWKS could not be retrieved from cache or remote URL.")
            raise AuthenticationFailed("Authentication unavailable (JWKS fetch failed).")

        payload = self._verify_token(raw_token, jwks_data)
        clerk_id = payload.get("sub")

        if not clerk_id:
            raise AuthenticationFailed("Token missing 'sub' claim.")

        try:
            user = User.objects.get(clerk_id=clerk_id)
        except User.DoesNotExist:
            user = self._provision_verified_user(clerk_id)

        if not user.is_active:
            logger.warning("Clerk account is inactive.")
            raise AuthenticationFailed("User account has been deactivated.")

        return (user, payload)

    def _provision_verified_user(self, clerk_id: str) -> User:
        """Close the gap between Clerk sign-up completion and webhook delivery."""
        secret = getattr(settings, "CLERK_SECRET_KEY", "")
        if not secret:
            raise AuthenticationFailed("Account setup is unavailable. Please contact support.")

        try:
            response = requests.get(
                f"https://api.clerk.com/v1/users/{quote(clerk_id, safe='')}",
                headers={"Authorization": f"Bearer {secret}"},
                timeout=5,
            )
            response.raise_for_status()
            clerk_user = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Clerk provisioning unavailable.")
            raise AuthenticationFailed("Account setup is still in progress. Please retry shortly.") from None

        if not isinstance(clerk_user, dict) or clerk_user.get("id") != clerk_id:
            raise AuthenticationFailed("Clerk returned an invalid account record.")

        # Reuse the webhook's email normalization, role whitelist, and profile creation.
        from .webhook_views import ClerkWebhookView

        try:
            # Same serialization key as signed webhook processing. Never resurrect
            # a deleted identity, including a delete racing server-side provisioning.
            from datetime import datetime, timezone
            from .models import ClerkWebhookState
            with transaction.atomic():
                state, _ = ClerkWebhookState.objects.select_for_update().get_or_create(
                    clerk_id=clerk_id, defaults={'last_event_at': datetime(1970, 1, 1, tzinfo=timezone.utc)})
                if state.is_deleted:
                    raise AuthenticationFailed('User account has been deactivated.')
                ClerkWebhookView()._handle_user_created(clerk_user)
        except ValidationError as exc:
            # exc.detail is typed as list | dict; narrow to dict before key access.
            if isinstance(exc.detail, dict) and "email" in exc.detail:
                message = exc.detail["email"]
                # DRF wraps string errors in a list of ErrorDetail objects.
                if isinstance(message, list) and message:
                    message = message[0]
                raise AuthenticationFailed(str(message)) from None
            raise AuthenticationFailed("Account setup failed. Please contact support.") from None
        except (TypeError, AttributeError, ValueError):
            raise AuthenticationFailed('Clerk returned an invalid account record.') from None
        except IntegrityError:
            # A simultaneous webhook may have created the same identity.
            pass

        try:
            return User.objects.get(clerk_id=clerk_id)
        except User.DoesNotExist as exc:
            raise AuthenticationFailed("This email is already registered to another account.") from None

    def _get_jwks(self, force_refresh: bool = False) -> dict[str, Any] | None:
        """One shared refresh attempt per 10s; cache TTL is the stale-key bound.

        No expired-key resurrection. If the shared refresh guard is unavailable,
        fail closed instead of issuing uncoordinated provider requests per token.
        """
        import hashlib
        url = getattr(settings, 'CLERK_JWKS_URL', '')
        def cached():
            try:
                value = cache.get(JWKS_CACHE_KEY)
                return value if self._valid_jwks(value) else None
            except Exception:
                logger.warning('Clerk JWKS cache unavailable.')
                return None
        current = cached()
        if current and not force_refresh:
            return current
        if not url:
            return None
        guard = 'clerk:jwks:refresh:' + hashlib.sha256(url.encode()).hexdigest()
        try:
            if not cache.add(guard, True, timeout=10):
                return current
        except Exception:
            logger.warning('Clerk JWKS refresh coordination unavailable.')
            return current
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            value = response.json()
            if not self._valid_jwks(value):
                raise ValueError('Invalid key set')
            try:
                cache.set(JWKS_CACHE_KEY, value, JWKS_CACHE_TTL)
            except Exception:
                logger.warning('Clerk JWKS cache write unavailable.')
            return value
        except Exception:
            logger.warning('Clerk JWKS refresh unavailable.')
            return current  # Only the original unexpired cache entry, never extended.

    @staticmethod
    def _valid_jwks(value):
        try:
            keys = value['keys']
            if not isinstance(keys, list) or not 1 <= len(keys) <= 16:
                return False
            if any(not isinstance(k, dict) or k.get('kty') != 'RSA' or
                   k.get('alg', 'RS256') != 'RS256' or k.get('use', 'sig') != 'sig' or
                   not isinstance(k.get('kid'), str) or not 1 <= len(k['kid']) <= 128 for k in keys):
                return False
            return len(jwt.PyJWKSet.from_dict(value).keys) == len(keys)
        except Exception:
            return False

    @sensitive_variables('token', 'payload')
    def _verify_token(self, token: str, jwks_data: dict[str, Any]) -> dict[str, Any]:
        """
        Verify the JWT RS256 signature and expiration against the provided JWKS data.
        """
        try:
            jwks_set = jwt.PyJWKSet.from_dict(jwks_data)
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")

            if not isinstance(kid, str) or not 1 <= len(kid) <= 128 or unverified_header.get("alg") != "RS256":
                raise AuthenticationFailed("Token header missing 'kid'.")

            # Resolve key matching kid
            signing_key = None
            for key in jwks_set.keys:
                if key.key_id == kid:
                    signing_key = key
                    break

            # If key not found, attempt forced JWKS refresh once (handles key rotation)
            if not signing_key:
                logger.info("Signing key not found in cached JWKS. Refreshing keys.")
                fresh_jwks = self._get_jwks(force_refresh=True)
                if fresh_jwks:
                    jwks_set = jwt.PyJWKSet.from_dict(fresh_jwks)
                    for key in jwks_set.keys:
                        if key.key_id == kid:
                            signing_key = key
                            break

            if not signing_key:
                raise AuthenticationFailed("Signing key not found in Clerk JWKS.")

            decode_options: dict[str, Any] = {"verify_aud": False, "require": ["exp", "sub", "iss"]}

            payload = jwt.decode(
                token,
                key=signing_key.key,
                algorithms=["RS256"],
                options=decode_options,
                issuer=settings.CLERK_ISSUER,
                # Allow small issuer/server clock differences, including iat/nbf.
                leeway=5,
            )
            # Enforce exact issuer equality independently of version-specific
            # PyJWT issuer/sequence handling; cryptographic verification stays above.
            if payload.get("iss") != settings.CLERK_ISSUER:
                raise AuthenticationFailed("Invalid issuer.")
            parties = getattr(settings, "CLERK_AUTHORIZED_PARTIES", [])
            if parties and payload.get("azp") not in parties:
                raise AuthenticationFailed("Invalid authorized party.")
            if payload.get("sts") == "pending":
                raise AuthenticationFailed("Session is pending.")
            return payload

        except ExpiredSignatureError:
            raise AuthenticationFailed("Token has expired.")
        except PyJWKSetError:
            logger.error("Clerk verification key format error.")
            raise AuthenticationFailed("Invalid token signature or key format.")
        except InvalidTokenError:
            logger.warning("Invalid Clerk credential presented.")
            raise AuthenticationFailed("Invalid token.") from None
        except AuthenticationFailed:
            raise
        except Exception:
            logger.error("Unexpected Clerk verification failure.")
            raise AuthenticationFailed("Token verification failed.")


try:
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

    class ClerkJWTScheme(OpenApiAuthenticationExtension):
        target_class = "interview_system.authentication.ClerkJWTAuthentication"
        name = "BearerAuth"

        def get_security_definition(self, auto_schema: Any) -> dict[str, Any]:
            return {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
            }
except ImportError:
    pass
