"""Opt-in InterviewAnalysis deployment overlay; no deployment is performed by this file."""
from urllib.parse import urlsplit
from django.core.exceptions import ImproperlyConfigured
from .settings import *  # noqa: F403


DEBUG = False
if not os.environ.get('DJANGO_SECRET_KEY') or len(os.environ['DJANGO_SECRET_KEY']) < 32:
    raise ImproperlyConfigured('Set a private DJANGO_SECRET_KEY of at least 32 characters.')
ALLOWED_HOSTS = [s.strip() for s in os.environ.get('INTERVIEW_ANALYSIS_DEPLOY_HOSTS', '').split(',') if s.strip()]
if not ALLOWED_HOSTS or any('*' in h or '/' in h or h.startswith('.') for h in ALLOWED_HOSTS):
    raise ImproperlyConfigured('Set explicit INTERVIEW_ANALYSIS_DEPLOY_HOSTS without wildcard domains.')
origins = [s.strip() for s in os.environ.get('INTERVIEW_ANALYSIS_DEPLOY_ORIGINS', '').split(',') if s.strip()]
def exact_https(value):
    parsed = urlsplit(value)
    return (parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
            and not parsed.path and not parsed.query and not parsed.fragment and '*' not in value)
if not origins or not all(exact_https(value) for value in origins):
    raise ImproperlyConfigured('Set exact HTTPS INTERVIEW_ANALYSIS_DEPLOY_ORIGINS without paths or wildcards.')
INTERVIEW_ANALYSIS_WS_ALLOWED_ORIGINS = origins
CORS_ALLOWED_ORIGINS = origins
CORS_ALLOWED_ORIGIN_REGEXES = []
CORS_ALLOW_ALL_ORIGINS = False
CSRF_TRUSTED_ORIGINS = origins
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
CELERY_TASK_DEFAULT_QUEUE = 'celery'
LOGGING = {
    'version': 1, 'disable_existing_loggers': False,
    'filters': {'credentials': {'()': 'ai_interview_recruitment.safe_logging.CredentialFilter'}},
    'formatters': {'safe': {'format': '%(levelname)s %(name)s %(message)s'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'filters': ['credentials'], 'formatter': 'safe'}},
    'root': {'handlers': ['console'], 'level': 'WARNING'},
    'loggers': {name: {'handlers': ['console'], 'level': 'WARNING', 'propagate': False}
                for name in ('django', 'django.request', 'django.server', 'daphne', 'celery')},
}
