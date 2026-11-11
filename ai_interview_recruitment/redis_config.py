"""Redis responsibilities; never infer process isolation from logical DB numbers."""
from urllib.parse import urlsplit
from django.core.exceptions import ImproperlyConfigured

ROLES = ('broker', 'results', 'cache', 'channels', 'guard', 'payload')
PORTS = dict(zip(ROLES, range(6380, 6386)))


def configure(namespace, environ, *, strict=False):
    urls = {}
    for role in ROLES:
        variable = {'results': 'RESULT_REDIS_URL', 'channels': 'CHANNEL_REDIS_URL'}.get(role, role.upper() + '_REDIS_URL')
        # Local defaults deliberately do not reuse the existing 6379 process.
        urls[role] = environ.get(variable, f'redis://127.0.0.1:{PORTS[role]}/0')
        if strict and not environ.get(variable):
            raise ImproperlyConfigured('Explicit ' + variable + ' is required.')
    endpoints = []
    for role, url in urls.items():
        try:
            parsed = urlsplit(url)
            endpoint = ((parsed.hostname or '').lower(), parsed.port or 6379)
            if parsed.scheme not in ('redis', 'rediss') or not endpoint[0] or parsed.query or parsed.fragment:
                raise ValueError()
        except ValueError:
            raise ImproperlyConfigured('Invalid Redis endpoint for ' + role) from None
        endpoints.append(endpoint)
    if len(set(endpoints)) != len(ROLES):
        raise ImproperlyConfigured('Redis responsibilities require distinct host/port endpoints; DB numbers are not isolation.')
    # Reject aliases that Celery could otherwise read ahead of app.conf.
    for alias, role in [('CELERY_BROKER_URL','broker'), ('CELERY_BROKER_READ_URL','broker'),
                        ('CELERY_BROKER_WRITE_URL','broker'), ('CELERY_RESULT_BACKEND','results'),
                        ('REDIS_URL','cache'), ('INTERVIEW_ANALYSIS_REDIS_URL','guard'),
                        ('INTERVIEW_ANALYSIS_PAYLOAD_REDIS_URL','payload')]:
        if environ.get(alias) and environ[alias] != urls[role]:
            raise ImproperlyConfigured(alias + ' conflicts with explicit Redis role configuration.')
    namespace.update({{'results': 'RESULT_REDIS_URL', 'channels': 'CHANNEL_REDIS_URL'}.get(role, role.upper() + '_REDIS_URL'): url for role, url in urls.items()})
    namespace.update(
        REDIS_URL=urls['cache'], CELERY_BROKER_URL=urls['broker'],
        CELERY_BROKER_READ_URL=urls['broker'], CELERY_BROKER_WRITE_URL=urls['broker'],
        CELERY_RESULT_BACKEND=urls['results'], CELERY_RESULT_EXPIRES=3600,
        CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS={'global_keyprefix': 'skillsense:results:'},
        INTERVIEW_ANALYSIS_REDIS_URL=urls['guard'], INTERVIEW_ANALYSIS_PAYLOAD_REDIS_URL=urls['payload'],
        CACHES={'default': {'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': urls['cache'], 'KEY_PREFIX': 'skillsense:cache', 'TIMEOUT': 300,
            'OPTIONS': {'socket_connect_timeout': 1, 'socket_timeout': 1}}},
        CHANNEL_LAYERS={'default': {'BACKEND': 'channels_redis.core.RedisChannelLayer', 'CONFIG': {
            'hosts': [{'address': urls['channels'], 'socket_connect_timeout': 1, 'socket_timeout': 1}],
            'prefix': 'skillsense:channels', 'capacity': 16, 'expiry': 10, 'group_expiry': 120,
            'serializer_format': 'json'}}},
    )
    return urls
