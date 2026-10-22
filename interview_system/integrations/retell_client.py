"""Retell web-call creation boundary; callers only use ``create_session``.

Configuration (resolved here on each call): RETELL_API_KEY and RETELL_AGENT_ID
in Django settings, falling back to environment variables when absent. An empty
setting intentionally disables the integration. No settings-file change is needed.

Agent prerequisite: configure a Retell Response Engine agent whose prompt includes
``{{interview_questions}}`` and instructs it to ask those approved questions in
order, one at a time, awaiting each answer. Metadata alone does not drive the agent.
Questions must be an ordered iterable of approved Question objects (or mappings
with text, category, approved, and interview_id), belonging to this interview.

create_session preserves the legacy call-ID contract. create_web_call returns
validated browser credentials for the authenticated join endpoint.
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

__all__ = ["create_session", "create_web_call", "get_call", "stop_call", "RetellUnavailableError"]

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


class RetellCreationUncertainError(RetellUnavailableError):
    """The provider may have created a call; reconcile before creating another."""


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
        item = {"text": text, "category": category}
        question_id = _field(question, 'pk') or _field(question, 'id')
        if question_id is not None:
            item['question_id'] = str(question_id)
        script.append(item)
    if not script:
        raise RetellUnavailableError("An approved question set is required.")
    return interview_id, json.dumps(script, ensure_ascii=False)


def _create_call(interview, questions) -> dict:
    """Create a call and validate its ID without logging provider responses."""
    api_key = _configuration("RETELL_API_KEY")
    agent_id = _configuration("RETELL_AGENT_ID")
    if getattr(settings, 'RETELL_CONVERSATION_GUARD_ENABLED', False):
        if not getattr(settings, 'RETELL_CONVERSATION_CONTROL_URL', ''):
            raise RetellUnavailableError('Guarded conversation callback is not configured.')
        verify_guard_configuration(api_key, agent_id)
        from ..models import InterviewConversation
        existing = InterviewConversation.objects.filter(interview_id=_field(interview, 'pk') or _field(interview, 'id')).first()
        if existing and existing.carried_evidence:
            remaining = {q['id'] for q in existing.questions[existing.cursor:]}
            questions = [q for q in questions if str(_field(q, 'pk') or _field(q, 'id')) in remaining]
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
        raise RetellCreationUncertainError("Retell session request outcome is unknown.") from None

    try:
        # Reject redirects as well as 4xx/5xx. Never forward auth to another URL.
        if not 200 <= response.status_code < 300:
            if response.status_code >= 500:
                raise RetellCreationUncertainError('Retell session request outcome is unknown.')
            raise RetellUnavailableError("Retell rejected session creation.")
        try:
            data = response.json()
        except ValueError:
            raise RetellCreationUncertainError("Retell returned an invalid response.") from None
        call_id = data.get("call_id") if isinstance(data, dict) else None
        if not isinstance(call_id, str) or not call_id.strip():
            raise RetellCreationUncertainError("Retell returned an invalid session ID.")
        return data
    finally:
        response.close()


def verify_guard_configuration(api_key, agent_id):
    """Fail closed before creating a call if provider configuration has drifted."""
    from urllib.parse import quote
    from ..conversation_protocol import PROMPT
    def read(path):
        try:
            response = requests.get('https://api.retellai.com/' + path,
                headers={'Authorization': f'Bearer {api_key}'}, timeout=(1.0, 2.0), allow_redirects=False)
        except requests.RequestException:
            raise RetellUnavailableError('Cannot verify the guarded agent.') from None
        try:
            if response.status_code != 200:
                raise RetellUnavailableError('Cannot verify the guarded agent.')
            return response.json()
        except ValueError:
            raise RetellUnavailableError('Invalid guarded agent configuration.') from None
        finally:
            response.close()
    agent = read('get-agent/' + quote(agent_id, safe=''))
    engine = agent.get('response_engine', {})
    if engine.get('type') != 'retell-llm' or not engine.get('llm_id'):
        raise RetellUnavailableError('Guarded agent response engine is incompatible.')
    llm = read('get-retell-llm/' + quote(engine['llm_id'], safe=''))
    tools = llm.get('general_tools') or []
    if (llm.get('general_prompt') != PROMPT or llm.get('states')
            or any(tool.get('type') == 'end_call' for tool in tools)
            or not any(tool.get('name') == 'interview_control'
                and tool.get('url') == settings.RETELL_CONVERSATION_CONTROL_URL for tool in tools)):
        raise RetellUnavailableError('Guarded agent configuration changed; operator review is required.')


def create_session(interview, questions) -> str:
    return _create_call(interview, questions)['call_id']


def create_web_call(interview, questions) -> dict:
    import time
    data = _create_call(interview, questions)
    if (not isinstance(data.get('access_token'), str) or not data['access_token'].strip()
            or data.get('transport') != 'gateway'
            or not isinstance(data.get('ice_servers'), list)
            or any(not isinstance(server, dict) or not server.get('urls') for server in data['ice_servers'])
            or type(data.get('expires_at')) is not int
            or data['expires_at'] <= int(time.time() * 1000)):
        raise RetellCreationUncertainError('Retell returned invalid browser credentials.')
    return {key: data[key] for key in ('call_id', 'access_token', 'transport', 'ice_servers', 'expires_at')}


def _call_request(method, operation, call_id):
    from urllib.parse import quote
    try:
        response = requests.request(
            method, f'https://api.retellai.com/v2/{operation}/{quote(call_id, safe="")}',
            headers={'Authorization': f'Bearer {_configuration("RETELL_API_KEY")}'},
            timeout=_REQUEST_TIMEOUT, allow_redirects=False,
        )
    except requests.RequestException:
        raise RetellUnavailableError('Retell call request failed.') from None
    try:
        if not 200 <= response.status_code < 300:
            raise RetellUnavailableError('Retell rejected the call request.')
        if operation == 'stop-call':
            return None
        try:
            data = response.json()
        except ValueError:
            raise RetellUnavailableError('Retell returned an invalid call.') from None
        if not isinstance(data, dict) or data.get('call_id') != call_id:
            raise RetellUnavailableError('Retell returned an invalid call.')
        return data
    finally:
        response.close()


def get_call(call_id):
    return _call_request('GET', 'get-call', call_id)


def stop_call(call_id):
    return _call_request('POST', 'stop-call', call_id)


def find_interview_calls(interview_id):
    """Reconcile an uncertain create using server-supplied metadata."""
    try:
        response = requests.post(
            'https://api.retellai.com/v3/list-calls',
            headers={'Authorization': f'Bearer {_configuration("RETELL_API_KEY")}'},
            json={'filter_criteria': {'metadata': [{'type': 'string', 'key': 'interview_id', 'value': str(interview_id)}]},
                  'sort_order': 'descending', 'limit': 100},
            timeout=_REQUEST_TIMEOUT, allow_redirects=False,
        )
    except requests.RequestException:
        raise RetellUnavailableError('Retell reconciliation failed.') from None
    try:
        if response.status_code != 200:
            raise RetellUnavailableError('Retell reconciliation failed.')
        try:
            data = response.json()
        except ValueError:
            raise RetellUnavailableError('Retell reconciliation failed.') from None
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise RetellUnavailableError('Retell reconciliation failed.')
        return [item for item in data['items'] if isinstance(item, dict)
                and item.get('agent_id') == _configuration('RETELL_AGENT_ID')
                and isinstance(item.get('metadata'), dict)
                and item['metadata'].get('interview_id') == str(interview_id)
                and isinstance(item.get('call_id'), str) and item['call_id']]
    finally:
        response.close()


def _credential_cipher():
    import base64
    import hashlib
    from cryptography.fernet import Fernet
    secret = getattr(settings, 'RETELL_CREDENTIAL_ENCRYPTION_KEY', '') or settings.SECRET_KEY
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def encrypt_credentials(data):
    return _credential_cipher().encrypt(json.dumps(data).encode()).decode()


def decrypt_credentials(value):
    from cryptography.fernet import InvalidToken
    try:
        return json.loads(_credential_cipher().decrypt(value.encode()))
    except (InvalidToken, ValueError, TypeError):
        raise RetellUnavailableError('Stored call credentials are unavailable.') from None


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
