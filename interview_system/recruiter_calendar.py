"""Google Calendar OAuth for a signed-in recruiter."""

import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

import requests
from cryptography.fernet import Fernet
from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import RecruiterCalendarConnection, RecruiterProfile, User
from .permissions import IsRecruiter


CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.freebusy"


def _calendar_configuration():
    client_id = settings.GOOGLE_CALENDAR_CLIENT_ID
    client_secret = settings.GOOGLE_CALENDAR_CLIENT_SECRET
    redirect_uri = settings.GOOGLE_CALENDAR_REDIRECT_URI
    try:
        redirect = urlsplit(redirect_uri)
        cipher = Fernet(settings.GOOGLE_CALENDAR_TOKEN_KEY)
    except (TypeError, ValueError):
        return None
    local_redirect = redirect.scheme == "http" and redirect.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        client_id and client_secret and redirect.hostname
        and (redirect.scheme == "https" or local_redirect)
        and not (redirect.username or redirect.password or redirect.query or redirect.fragment)
    ):
        return client_id, client_secret, redirect_uri, cipher
    return None


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _code_verifier(state):
    return salted_hmac("google-calendar-pkce", state, algorithm="sha256").hexdigest()


class CalendarConnectView(APIView):
    permission_classes = [IsAuthenticated, IsRecruiter]

    def post(self, request):
        config = _calendar_configuration()
        if config is None:
            return Response({"detail": "Google Calendar is not configured on this server."}, status=503)
        client_id, _, redirect_uri, _ = config
        state = secrets.token_urlsafe(32)
        verifier = _code_verifier(state)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        with transaction.atomic():
            user = get_object_or_404(User.objects.select_for_update(), pk=request.user.pk, is_active=True)
            profile = get_object_or_404(RecruiterProfile, user=user)
            RecruiterCalendarConnection.objects.update_or_create(
                recruiter=profile,
                defaults={
                    "state_digest": _digest(state),
                    "state_expires_at": timezone.now() + timedelta(minutes=10),
                },
            )
        query = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": CALENDAR_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge.rstrip(b"=").decode(),
            "code_challenge_method": "S256",
        })
        return Response(
            {"authorization_url": f"https://accounts.google.com/o/oauth2/v2/auth?{query}"},
            headers={"Cache-Control": "private, no-store"},
        )


def _result(message, status=200):
    response = HttpResponse(
        f"<!doctype html><html lang='en'><meta charset='utf-8'>"
        f"<title>SkillSense Calendar</title><h1>{message}</h1>"
        "<p>You can close this tab and return to Workspace setup in SkillSense.</p></html>",
        status=status,
    )
    response["Referrer-Policy"] = "no-referrer"
    return response


@never_cache
@require_GET
def calendar_callback(request):
    config = _calendar_configuration()
    if config is None:
        return _result("Google Calendar is unavailable.", 503)

    state = request.GET.get("state", "")
    if len(state) != 43:
        return _result("This connection request is invalid or expired.", 400)
    pending = RecruiterCalendarConnection.objects.filter(
        state_digest=_digest(state), recruiter__user__is_active=True,
    )
    # Claim this state exactly once. A newer connect attempt replaces its digest.
    if pending.filter(state_expires_at__gt=timezone.now()).update(state_expires_at=None) != 1:
        return _result("This connection request is invalid or expired.", 400)
    code = request.GET.get("code")
    if not code or request.GET.get("error"):
        return _result("Google Calendar access was not granted.", 400)

    client_id, client_secret, redirect_uri, cipher = config
    try:
        token_response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": _code_verifier(state),
            },
            timeout=10,
            allow_redirects=False,
        )
        if token_response.status_code != 200:
            return _result("Google could not complete authorization. Start again in SkillSense.", 502)
        tokens = token_response.json()
        refresh_token = tokens.get("refresh_token")
        scope = tokens.get("scope", "")
        if not isinstance(scope, str) or CALENDAR_SCOPE not in scope.split():
            return _result("Google Calendar availability permission was not granted.", 400)
        if not isinstance(refresh_token, str) or not refresh_token:
            return _result("Google did not provide ongoing Calendar access. Start again in SkillSense.", 502)
    except (requests.RequestException, ValueError, AttributeError):
        return _result("Google could not complete authorization. Start again in SkillSense.", 502)

    updated = pending.update(
        state_digest=None,
        encrypted_refresh_token=cipher.encrypt(refresh_token.encode()),
        connected_at=timezone.now(),
    )
    if not updated:
        return _result("This connection request was replaced or cancelled.", 400)
    return _result("Google Calendar connected.")
