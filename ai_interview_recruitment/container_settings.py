"""Explicit container overlay; local foundation is loopback-only behind nginx.

Use production_settings/TLS policy before any public release. No dotenv file is
copied into images; Compose injects the selected environment at runtime.
"""
from .settings import *  # noqa: F403
from django.core.exceptions import ImproperlyConfigured

DEBUG = False
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
if len(SECRET_KEY) < 32:
    raise ImproperlyConfigured('Container DJANGO_SECRET_KEY must be at least 32 characters.')
ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,web,nginx').split(',')
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_ROOT = BASE_DIR / 'media'
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}

# Redis role settings are shared with the production and local configuration.
from .redis_config import configure as _configure_redis
_configure_redis(globals(), os.environ, strict=True)
CELERY_TASK_DEFAULT_QUEUE = CELERY_TASK_DEFAULT_ROUTING_KEY = CELERY_TASK_DEFAULT_EXCHANGE = 'celery'
CELERY_TASK_ALWAYS_EAGER = False
CELERY_BROKER_TRANSPORT_OPTIONS = {'socket_timeout': 2, 'socket_connect_timeout': 2, 'visibility_timeout': 3600}
CELERY_WORKER_ENABLE_REMOTE_CONTROL = True
INTERVIEW_ANALYSIS_MODEL_DIRECTORY = os.environ.get('INTERVIEW_ANALYSIS_MODEL_DIRECTORY', '/models')
DATABASES['default']['OPTIONS'] = {'connect_timeout': 2}
# No public transport/TLS assertion is made by the loopback-only local stack.
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_HTTPONLY = True
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGIN_REGEXES = []
CORS_ALLOWED_ORIGINS = INTERVIEW_ANALYSIS_WS_ALLOWED_ORIGINS
# Ticket query strings must never enter ASGI access logs or exception traces.
LOGGING = {
    'version': 1, 'disable_existing_loggers': False,
    'filters': {'credentials': {'()': 'ai_interview_recruitment.safe_logging.CredentialFilter'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'filters': ['credentials']}},
    'root': {'handlers': ['console'], 'level': 'WARNING'},
    'loggers': {name: {'handlers': ['console'], 'level': 'WARNING', 'propagate': False}
                for name in ('django', 'django.request', 'django.server', 'daphne', 'celery')},
}
HEALTH_REDIS_URLS = (CELERY_BROKER_URL, CELERY_RESULT_BACKEND, REDIS_URL,
                     CHANNEL_REDIS_URL, INTERVIEW_ANALYSIS_REDIS_URL)
if INTERVIEW_ANALYSIS_VISION_ENABLED:
    HEALTH_REDIS_URLS += (INTERVIEW_ANALYSIS_PAYLOAD_REDIS_URL,)
# Worker-side evidence for the trivial deployment smoke task.
CELERY_IMPORTS = ('interview_system.tasks.deployment',)


# Missing auth configuration is a startup failure, not a permissive runtime mode.
from .clerk_config import validate as _validate_clerk
CLERK_ISSUER = os.environ.get('CLERK_ISSUER', '')
CLERK_REQUIRE_SECURE_CONFIG = True
_validate_clerk(globals())
