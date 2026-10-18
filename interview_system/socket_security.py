"""Exact browser origins and credential scrubbing before Channels sees a scope."""
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.http.request import validate_host
from django.views.decorators.debug import sensitive_variables


def origin_key(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
                or '*' in value or any(c.isspace() for c in value)):
            return None
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return None
        return parsed.scheme, parsed.hostname.lower(), port if port is not None else (443 if parsed.scheme == 'https' else 80)
    except ValueError:
        return None


class SecureSocketEntry:
    def __init__(self, application):
        self.application = application

    @sensitive_variables()
    async def __call__(self, scope, receive, send):
        headers = scope.get('headers', [])
        origins = [v for k, v in headers if k.lower() == b'origin']
        hosts = [v for k, v in headers if k.lower() == b'host']
        allowed = False
        try:
            if len(origins) == len(hosts) == 1:
                origin = origin_key(origins[0].decode('ascii'))
                host = urlsplit('//' + hosts[0].decode('ascii'))
                # Only a hostname/optional port, not userinfo or a URL path.
                host_valid = (host.hostname and not host.username and not host.password and not host.path
                              and not host.query and not host.fragment and host.port != 0)
                allowed = (origin is not None and host_valid and validate_host(host.hostname, settings.ALLOWED_HOSTS)
                           and origin in [origin_key(o) for o in settings.INTERVIEW_ANALYSIS_WS_ALLOWED_ORIGINS])
        except (ValueError, UnicodeDecodeError):
            pass
        query = scope.get('query_string', b'')
        ticket = re.fullmatch(rb'ticket=([A-Za-z0-9_-]{43})', query)
        route = re.fullmatch(r'/ws/analysis/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/(?:observe/)?', scope.get('path', ''))
        if scope.get('interview_analysis_bounded_input') is not True or not allowed or not ticket or not route:
            await receive()  # ASGI requires a connect event before denial.
            await send({'type': 'websocket.close', 'code': 4403})
            return
        clean = dict(scope)
        clean['query_string'] = b''
        clean.pop('raw_path', None)
        clean['subprotocols'] = []
        clean['headers'] = [(k, v) for k, v in headers if k.lower() in (b'host', b'origin')]
        clean['interview_analysis_ticket'] = ticket[1].decode('ascii')
        await self.application(clean, receive, send)
