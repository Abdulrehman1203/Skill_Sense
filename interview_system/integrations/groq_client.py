"""Bounded Groq JSON requests. Credentials and provider payloads stay private."""
import json

import requests
from django.conf import settings


def configured():
    return bool(settings.GROQ_API_KEY)


def generate_json(prompt, *, instruction='', schema=None, model=None, timeout=15, tokens=4096):
    from .gemini_client import EvaluationProviderError

    if not configured():
        raise EvaluationProviderError('PROVIDER_UNCONFIGURED')
    output = {'type': 'json_object'}
    if schema:
        output = {'type': 'json_schema', 'json_schema': {
            'name': 'result', 'strict': True, 'schema': schema}}
    if schema and 'answers' in schema.get('properties', {}):
        instruction += (
            '\nEvidence quotes must be exact contiguous substrings copied from the referenced turn text. '
            'Preserve spelling, punctuation, whitespace and disfluencies. Never combine separate passages '
            'or insert ellipses. Prefer one short exact quote of 5 to 15 words for each cited turn.'
        )
    messages = [{'role': 'system', 'content': instruction or 'Return valid JSON matching the requested format.'},
                {'role': 'user', 'content': prompt}]
    try:
        response = requests.post('https://api.groq.com/openai/v1/chat/completions',
            headers={'Authorization': 'Bearer ' + settings.GROQ_API_KEY},
            json={'model': model or settings.GROQ_MODEL_NAME, 'messages': messages,
                  'response_format': output, 'max_completion_tokens': tokens},
            timeout=(3, timeout), allow_redirects=False)
    except requests.RequestException:
        raise EvaluationProviderError('PROVIDER_CONNECTION_FAILED', transient=True) from None
    with response:
        if response.status_code != 200:
            code = {429: 'PROVIDER_RATE_LIMITED', 503: 'PROVIDER_BUSY',
                    401: 'PROVIDER_ACCESS_DENIED', 403: 'PROVIDER_ACCESS_DENIED',
                    400: 'PROVIDER_REQUEST_REJECTED'}.get(response.status_code, 'PROVIDER_UNAVAILABLE')
            raise EvaluationProviderError(code, transient=response.status_code in (408, 429, 500, 502, 503, 504))
        try:
            text = response.json()['choices'][0]['message']['content']
            return json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        except (ValueError, KeyError, IndexError, TypeError):
            raise EvaluationProviderError('PROVIDER_INVALID_OUTPUT') from None
