"""Bounded labels, no paths, query strings, user identifiers, bodies or results."""
from collections import defaultdict
import threading
import time
from uuid import UUID, uuid4
from django.conf import settings
from django.http import JsonResponse
from .safe_logging import request_id, emit_event

_lock = threading.Lock()
_http = defaultdict(lambda: {'count': 0, 'seconds': 0., 'errors': 0})


def correlation(value):
    try:
        return UUID(str(value)).hex
    except (ValueError, TypeError, AttributeError):
        return uuid4().hex


def route_bucket(request):
    match = getattr(request, 'resolver_match', None)
    name = getattr(match, 'url_name', '') or ''
    if not name:
        return 'unmatched'
    for prefix, bucket in (('candidate-resume','resume'), ('resume','resume'), ('application','application'),
                           ('health','health'), ('metrics','metrics'), ('login','auth'), ('me','auth')):
        if name.startswith(prefix):
            return bucket
    return 'other'


class RequestEventsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = request_id.set(correlation(request.headers.get('X-Request-ID')))
        started = time.monotonic()
        status = 500
        try:
            try:
                size = int(request.META.get('CONTENT_LENGTH') or 0)
            except (ValueError, TypeError):
                size = -1
            if size < 0:
                response = JsonResponse({'detail': 'Invalid request length.'}, status=400)
            elif size > settings.MAX_UPLOAD_REQUEST_BYTES:
                response = JsonResponse({'detail': 'Request exceeds the 6 MiB upload envelope.'}, status=413)
            else:
                response = self.get_response(request)
            status = response.status_code
            response['X-Request-ID'] = request_id.get()
            return response
        finally:
            elapsed = max(0., time.monotonic() - started)
            route = route_bucket(request)
            with _lock:
                item = _http[route]
                item['count'] += 1
                item['seconds'] += elapsed
                item['errors'] += int(status >= 500)
            method = request.method if request.method in {'GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS'} else 'OTHER'
            emit_event('http_completed', component='http', route=route, method=method,
                       status=status, duration_ms=elapsed * 1000)
            request_id.reset(token)


def http_metrics():
    with _lock:
        return {key: dict(value) for key, value in _http.items()}
