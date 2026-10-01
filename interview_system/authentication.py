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
from django.db import IntegrityError
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

    def authenticate(self, request: Any) -> tuple[User, dict[str, Any]] | None:
        auth_header = get_authorization_header(request).split()

        if not auth_header or auth_header[0].lower() != b"bearer":
            return None

        if len(auth_header) == 1:
            raise AuthenticationFailed("Invalid bearer header. No token provided.")
        elif len(auth_header) > 2:
            raise AuthenticationFailed("Invalid bearer header. Token contains spaces.")

        raw_token = auth_header[1].decode("utf-8")
        return self.authenticate_credentials(raw_token)

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
            logger.warning("Authentication failed: User with clerk_id=%s is deactivated.", clerk_id)
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
            logger.warning("Could not load Clerk user %s for provisioning: %s", clerk_id, type(exc).__name__)
            raise AuthenticationFailed("Account setup is still in progress. Please retry shortly.") from exc

        if not isinstance(clerk_user, dict) or clerk_user.get("id") != clerk_id:
            raise AuthenticationFailed("Clerk returned an invalid account record.")

        # Reuse the webhook's email normalization, role whitelist, and profile creation.
        from .webhook_views import ClerkWebhookView

        try:
            ClerkWebhookView()._handle_user_created(clerk_user)
        except ValidationError as exc:
            # exc.detail is typed as list | dict; narrow to dict before key access.
            if isinstance(exc.detail, dict) and "email" in exc.detail:
                message = exc.detail["email"]
                # DRF wraps string errors in a list of ErrorDetail objects.
                if isinstance(message, list) and message:
                    message = message[0]
                raise AuthenticationFailed(str(message)) from exc
            raise AuthenticationFailed("Account setup failed. Please contact support.") from exc
        except IntegrityError:
            # A simultaneous webhook may have created the same identity.
            pass

        try:
            return User.objects.get(clerk_id=clerk_id)
        except User.DoesNotExist as exc:
            raise AuthenticationFailed("This email is already registered to another account.") from exc

    def _get_jwks(self, force_refresh: bool = False) -> dict[str, Any] | None:
        """
        Fetch JWKS dict from Redis cache, or fetch from CLERK_JWKS_URL on cache miss.
        """
        if not force_refresh:
            try:
                cached_jwks = cache.get(JWKS_CACHE_KEY)
                if cached_jwks:
                    return cached_jwks
            except Exception as e:
                logger.warning("Cache read failed (Redis connection error?): %s", e)

        jwks_url = getattr(settings, "CLERK_JWKS_URL", "")
        if not jwks_url:
            logger.error("CLERK_JWKS_URL setting is missing or empty.")
            return None

        try:
            response = requests.get(jwks_url, timeout=10)
            response.raise_for_status()
            jwks_data = response.json()
            try:
                cache.set(JWKS_CACHE_KEY, jwks_data, JWKS_CACHE_TTL)
            except Exception as e:
                logger.warning("Cache write failed (Redis connection error?): %s", e)
            return jwks_data
        except Exception as exc:
            logger.error("Failed to fetch Clerk JWKS from URL %s: %s", jwks_url, exc)
            # Stale fallback attempt if force_refresh failed
            try:
                stale_jwks = cache.get(JWKS_CACHE_KEY)
                if stale_jwks:
                    logger.info("Using stale cached JWKS as fallback.")
                    return stale_jwks
            except Exception:
                pass
            return None

    def _verify_token(self, token: str, jwks_data: dict[str, Any]) -> dict[str, Any]:
        """
        Verify the JWT RS256 signature and expiration against the provided JWKS data.
        """
        try:
            jwks_set = jwt.PyJWKSet.from_dict(jwks_data)
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")

            if not kid:
                raise AuthenticationFailed("Token header missing 'kid'.")

            # Resolve key matching kid
            signing_key = None
            for key in jwks_set.keys:
                if key.key_id == kid:
                    signing_key = key
                    break

            # If key not found, attempt forced JWKS refresh once (handles key rotation)
            if not signing_key:
                logger.info("Key ID %s not found in cached JWKS. Forcing JWKS refresh...", kid)
                fresh_jwks = self._get_jwks(force_refresh=True)
                if fresh_jwks:
                    jwks_set = jwt.PyJWKSet.from_dict(fresh_jwks)
                    for key in jwks_set.keys:
                        if key.key_id == kid:
                            signing_key = key
                            break

            if not signing_key:
                raise AuthenticationFailed("Signing key not found in Clerk JWKS.")

            decode_options: dict[str, Any] = {"verify_aud": False}
            if getattr(settings, "DEBUG", False):
                decode_options["verify_exp"] = False

            payload = jwt.decode(
                token,
                key=signing_key.key,
                algorithms=["RS256"],
                options=decode_options,
            )
            return payload

        except ExpiredSignatureError:
            raise AuthenticationFailed("Token has expired.")
        except PyJWKSetError as e:
            logger.error("PyJWKSet verification error: %s", e)
            raise AuthenticationFailed("Invalid token signature or key format.")
        except InvalidTokenError as e:
            logger.warning("Invalid token presented: %s", e)
            raise AuthenticationFailed(f"Invalid token: {str(e)}")
        except AuthenticationFailed:
            raise
        except Exception as e:
            logger.error("Unexpected error verifying token: %s", e)
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
