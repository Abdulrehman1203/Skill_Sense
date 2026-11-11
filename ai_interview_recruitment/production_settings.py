"""Opt-in InterviewAnalysis deployment overlay; no deployment is performed by this file."""
from urllib.parse import urlsplit
from django.core.exceptions import ImproperlyConfigured
from .settings import *  # noqa: F403


DEBUG = False
if not os.environ.get('DJANGO_SECRET_KEY') or len(os.environ['DJANGO_SECRET_KEY']) < 32:
    raise ImproperlyConfigured('Set a private DJANGO_SECRET_KEY of at least 32 characters.')
ALLOWED_HOSTS = [s.strip() for s in os.environ.get('INTERVIEW_ANALYSIS_DEPLOY_HOSTS', '').split(',') if s.strip()]
if not ALLOWED_HOSTS or any('*' in h or '/' in h or h.startswith('.') or ':' in h or '@' in h
                            or any(c.isspace() for c in h) for h in ALLOWED_HOSTS):
    raise ImproperlyConfigured('Set explicit INTERVIEW_ANALYSIS_DEPLOY_HOSTS without wildcard domains.')
origins = [s.strip() for s in os.environ.get('INTERVIEW_ANALYSIS_DEPLOY_ORIGINS', '').split(',') if s.strip()]
def exact_https(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
        return (parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
                and not parsed.path and not parsed.query and not parsed.fragment and '*' not in value
                and not any(c.isspace() for c in value) and (port is None or 1 <= port <= 65535))
    except ValueError:
        return False
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
def policy_bool(name):
    value = os.environ.get(name, 'false').lower()
    if value not in ('true', 'false'):
        raise ImproperlyConfigured(name + ' must be true or false.')
    return value == 'true'
SECURE_HSTS_INCLUDE_SUBDOMAINS = policy_bool('PRODUCTION_HSTS_INCLUDE_SUBDOMAINS')
SECURE_HSTS_PRELOAD = policy_bool('PRODUCTION_HSTS_PRELOAD')
if SECURE_HSTS_PRELOAD and not SECURE_HSTS_INCLUDE_SUBDOMAINS:
    raise ImproperlyConfigured('HSTS preload requires reviewed subdomain coverage.')
CELERY_TASK_DEFAULT_QUEUE = 'celery'
CELERY_TASK_ALWAYS_EAGER = False
from .safe_logging import production_logging
LOGGING = production_logging()
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_WORKER_REDIRECT_STDOUTS = False

# Revalidate after DEBUG=False; never inherit local endpoint assumptions.
from .redis_config import configure as _configure_redis
_configure_redis(globals(), os.environ, strict=True)

# Missing auth configuration is a startup failure, not a permissive runtime mode.
from .clerk_config import validate as _validate_clerk
CLERK_ISSUER = os.environ.get('CLERK_ISSUER', '')
for _key in ('CLERK_JWKS_URL', 'CLERK_SECRET_KEY', 'CLERK_WEBHOOK_SECRET'):
    globals()[_key] = os.environ.get(_key, '')
CLERK_AUTHORIZED_PARTIES = [value.strip() for value in os.environ.get('CLERK_AUTHORIZED_PARTIES', '').split(',') if value.strip()]
CLERK_REQUIRE_SECURE_CONFIG = True
_validate_clerk(globals())

# Explicit production secrets and storage; no local dotenv/default credentials.
from copy import deepcopy
import ssl

def required(name, minimum=1):
    value = os.environ.get(name, '').strip()
    if len(value) < minimum or value.startswith(('django-insecure-', 'replace-', 'changeme')):
        raise ImproperlyConfigured('Required production setting: ' + name)
    return value

SECRET_KEY = required('DJANGO_SECRET_KEY', 50)
if len(set(SECRET_KEY)) < 5:
    raise ImproperlyConfigured('DJANGO_SECRET_KEY must have sufficient entropy.')
for _key in ('DB_HOST', 'DB_NAME', 'DB_USER', 'DB_PASSWORD', 'GEMINI_API_KEY',
             'RETELL_API_KEY', 'RETELL_AGENT_ID', 'RETELL_WEBHOOK_SECRET', 'METRICS_TOKEN'):
    globals()[_key] = required(_key, 32 if _key == 'METRICS_TOKEN' else 1)
RETELL_CREDENTIAL_ENCRYPTION_KEY = required('RETELL_CREDENTIAL_ENCRYPTION_KEY', 32)
if RETELL_CREDENTIAL_ENCRYPTION_KEY == SECRET_KEY:
    raise ImproperlyConfigured('Use a separate RETELL_CREDENTIAL_ENCRYPTION_KEY.')
DATABASES = deepcopy(DATABASES)
DATABASES['default'].update({key: os.environ['DB_' + key] for key in ('HOST','NAME','USER','PASSWORD')})
DATABASES['default']['OPTIONS'] = {'sslmode': 'verify-full', 'connect_timeout': 3}
if os.environ.get('DB_SSLROOTCERT'):
    DATABASES['default']['OPTIONS']['sslrootcert'] = os.environ['DB_SSLROOTCERT']
for _key in ('BROKER_REDIS_URL','RESULT_REDIS_URL','CACHE_REDIS_URL','CHANNEL_REDIS_URL','GUARD_REDIS_URL','PAYLOAD_REDIS_URL'):
    if urlsplit(globals()[_key]).scheme != 'rediss':
        raise ImproperlyConfigured('Production requires certificate-verified TLS for ' + _key)
CELERY_BROKER_USE_SSL = {'ssl_cert_reqs': ssl.CERT_REQUIRED}
CELERY_REDIS_BACKEND_USE_SSL = {'ssl_cert_reqs': ssl.CERT_REQUIRED}
SESSION_COOKIE_HTTPONLY = CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = CSRF_COOKIE_SAMESITE = 'Lax'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
USE_X_FORWARDED_HOST = False
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = '/private-media-disabled/'
PRIVATE_STORAGE_BACKEND = required('PRIVATE_STORAGE_BACKEND')
if PRIVATE_STORAGE_BACKEND == 'filesystem':
    MEDIA_ROOT = Path(required('PRIVATE_MEDIA_ROOT'))
    if not MEDIA_ROOT.is_absolute() or MEDIA_ROOT == STATIC_ROOT or STATIC_ROOT in MEDIA_ROOT.parents:
        raise ImproperlyConfigured('PRIVATE_MEDIA_ROOT must be absolute and outside STATIC_ROOT.')
    STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage', 'OPTIONS': {'file_permissions_mode': 0o644, 'directory_permissions_mode': 0o755}}}
elif PRIVATE_STORAGE_BACKEND == 's3':
    _kms = required('AWS_S3_KMS_KEY_ID')
    STORAGES = {'default': {'BACKEND': 'storages.backends.s3.S3Storage', 'OPTIONS': {
        'bucket_name': required('AWS_STORAGE_BUCKET_NAME'), 'region_name': required('AWS_S3_REGION_NAME'),
        'default_acl': None, 'custom_domain': None, 'querystring_auth': True, 'querystring_expire': 60,
        'file_overwrite': False, 'object_parameters': {'ServerSideEncryption': 'aws:kms', 'SSEKMSKeyId': _kms}}},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage', 'OPTIONS': {'file_permissions_mode': 0o644, 'directory_permissions_mode': 0o755}}}
else:
    raise ImproperlyConfigured('PRIVATE_STORAGE_BACKEND must be filesystem or s3.')
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = required('EMAIL_HOST')
EMAIL_HOST_USER = required('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = required('EMAIL_HOST_PASSWORD')
DEFAULT_FROM_EMAIL = required('DEFAULT_FROM_EMAIL')
EMAIL_USE_TLS = True
EMAIL_USE_SSL = False
EMAIL_TIMEOUT = 10
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
HEALTH_CHECK_MIGRATIONS = True
HEALTH_CHECK_VISION = True
HEALTH_REDIS_URLS = (CELERY_BROKER_URL, CELERY_RESULT_BACKEND, CACHE_REDIS_URL, CHANNEL_REDIS_URL,
                     INTERVIEW_ANALYSIS_REDIS_URL, INTERVIEW_ANALYSIS_PAYLOAD_REDIS_URL)
MIDDLEWARE = ['ai_interview_recruitment.operations.RequestEventsMiddleware'] + MIDDLEWARE
RETENTION_RESUME_DAYS = int(os.environ.get('RETENTION_RESUME_DAYS', '90'))
RETENTION_TRANSCRIPT_DAYS = int(os.environ.get('RETENTION_TRANSCRIPT_DAYS', '90'))
RETENTION_LOG_DAYS = int(os.environ.get('RETENTION_LOG_DAYS', '30'))
if not 1 <= INTERVIEW_ANALYSIS_FRAME_DEADLINE_SECONDS <= 60:
    raise ImproperlyConfigured('Production private frame payload lifetime must be 1..60 seconds.')
if min(RETENTION_RESUME_DAYS, RETENTION_TRANSCRIPT_DAYS, RETENTION_LOG_DAYS) < 1:
    raise ImproperlyConfigured('Retention review windows must be positive days.')
# These are inventory/review thresholds; immutable records and legal holds are
# never bypassed by an automatic purge in this change.
