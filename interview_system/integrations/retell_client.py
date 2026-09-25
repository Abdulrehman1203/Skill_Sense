"""Retell web-call creation boundary; callers only use ``create_session``.

Configuration (resolved here on each call): RETELL_API_KEY and RETELL_AGENT_ID
in Django settings, falling back to environment variables when absent. An empty
setting intentionally disables the integration. No settings-file change is needed.

Agent prerequisite: configure a Retell Response Engine agent whose prompt includes
``{{interview_questions}}`` and instructs it to ask those approved questions in
order, one at a time, awaiting each answer. Metadata alone does not drive the agent.
Questions must be an ordered iterable of approved Question objects (or mappings
with text, category, approved, and interview_id), belonging to this interview.

The returned string is Retell's call_id, not a browser access token. Browser join
credentials returned by Retell are deliberately not exposed by this Step 1 contract.
No models are saved, no tasks scheduled, and no raw provider errors are logged.

API reference: https://docs.retellai.com/api-references/create-web-call
Tests patch ``interview_system.integrations.retell_client.requests.post``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

import requests
from django.conf import settings

__all__ = ["create_session", "RetellUnavailableError"]

_CREATE_WEB_CALL_URL = "https://api.retellai.com/v3/create-web-call"
# Requests timeouts bound connection and socket inactivity, not total wall time.
# No retry loop: a timeout can occur after Retell has already created the call.
_REQUEST_TIMEOUT = (2.0, 5.0)
_CATEGORIES = frozenset({"BEHAVIORAL", "TECHNICAL", "SITUATIONAL"})


class RetellUnavailableError(Exception):
    """Configuration, input, transport, API, or response prevented session creation.

    Safe for callers to catch for graceful degradation. Messages are static and
    never contain credentials, request bodies, response bodies, or provider errors.
    Unexpected programming errors are deliberately not swallowed.
    """


def _configuration(name: str) -> str:
    value = getattr(settings, name, os.environ.get(name, ""))
    if not isinstance(value, str) or not value.strip():
        raise RetellUnavailableError("Retell integration is not configured.")
    return value.strip()


def _field(obj, name):
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def _question_context(interview, questions) -> tuple[str, str]:
    interview_id = _field(interview, "pk") or _field(interview, "id")
    if interview_id is None or not str(interview_id).strip():
        raise RetellUnavailableError("A saved interview is required.")
    interview_id = str(interview_id)
    if questions is None or isinstance(questions, (str, bytes, Mapping)):
        raise RetellUnavailableError("An approved question set is required.")
    try:
        items = iter(questions)
    except TypeError:
        raise RetellUnavailableError("An approved question set is required.") from None
    script = []
    for question in items:
        text = _field(question, "text")
        category = _field(question, "category")
        if (
            _field(question, "approved") is not True
            or str(_field(question, "interview_id")) != interview_id
            or not isinstance(text, str)
            or not text.strip()
            or not isinstance(category, str)
            or category not in _CATEGORIES
        ):
            raise RetellUnavailableError("The approved question set is invalid.")
        script.append({"text": text, "category": category})
    if not script:
        raise RetellUnavailableError("An approved question set is required.")
    return interview_id, json.dumps(script, ensure_ascii=False)


def create_session(interview, questions) -> str:
    """Create one Retell voice session for the supplied approved question set.

    Returns a non-empty call_id string. Expected failures raise
    RetellUnavailableError, never None. To degrade, catch only that exception.
    The call is mockable at requests.post; no HTTP client is captured at import.
    """
    api_key = _configuration("RETELL_API_KEY")
    agent_id = _configuration("RETELL_AGENT_ID")
    interview_id, context = _question_context(interview, questions)
    payload = {
        "agent_id": agent_id,
        "metadata": {"interview_id": interview_id},
        "retell_llm_dynamic_variables": {"interview_questions": context},
    }
    # Suppress provider exception chains: even a transport exception may embed
    # Authorization or response tokens. Never interpolate its text into ours.
    try:
        response = requests.post(
            _CREATE_WEB_CALL_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=_REQUEST_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException:
        raise RetellUnavailableError("Retell session request failed.") from None

    try:
        # Reject redirects as well as 4xx/5xx. Never forward auth to another URL.
        if not 200 <= response.status_code < 300:
            raise RetellUnavailableError("Retell rejected session creation.")
        try:
            data = response.json()
        except ValueError:
            raise RetellUnavailableError("Retell returned an invalid response.") from None
        call_id = data.get("call_id") if isinstance(data, dict) else None
        if not isinstance(call_id, str) or not call_id.strip():
            raise RetellUnavailableError("Retell returned an invalid session ID.")
        return call_id
    finally:
        response.close()


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    """Retell HMAC-SHA256 over raw body + millisecond timestamp, five-minute TTL.

    Matches Retell's SDK webhook_auth.py; keeps API-key access in this module.
    Does not decode or parse the unverified body and never logs signatures/keys.
    """
    import hashlib
    import hmac
    import re
    import time

    match = re.fullmatch(r'v=(\d{1,16}),d=([0-9a-f]{64})', signature)
    if match is None:
        return False
    stamp = int(match.group(1))
    if abs(int(time.time() * 1000) - stamp) > 300_000:
        return False
    try:
        key = _configuration('RETELL_API_KEY')
    except RetellUnavailableError:
        return False
    expected = hmac.new(key.encode(), body + str(stamp).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, match.group(2))
