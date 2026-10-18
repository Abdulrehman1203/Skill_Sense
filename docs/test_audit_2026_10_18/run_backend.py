"""Reproducible full-discovery audit. No application migrations or provider calls."""
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
OUT = Path(__file__).resolve().parent
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_interview_recruitment.settings')
from django.conf import settings

prefix = 'full-audit:'+uuid.uuid4().hex
settings.DATABASES['default']['TEST'] = {'NAME': 'test_full_audit_'+uuid.uuid4().hex[:12]}
settings.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': prefix}}
settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
settings.CELERY_BROKER_URL = 'redis://127.0.0.1:6379/15'
settings.CELERY_RESULT_BACKEND = 'redis://127.0.0.1:6379/15'
settings.CELERY_TASK_DEFAULT_QUEUE = prefix+':general'
if os.environ.get('AUDIT_MINIMAL_ROUTING') != '1':
    settings.CELERY_TASK_DEFAULT_EXCHANGE = prefix+':general'
    settings.CELERY_TASK_DEFAULT_ROUTING_KEY = prefix+':general'
settings.CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS = {'global_keyprefix': prefix+':results:'}

secrets = [str(getattr(settings, key)) for key in dir(settings)
           if any(word in key for word in ('SECRET', 'PASSWORD', 'API_KEY', 'TOKEN'))
           and isinstance(getattr(settings, key), str) and len(getattr(settings, key)) >= 8]
def clean(text):
    text = str(text)
    for value in secrets:
        text = text.replace(value, '[PRIVATE]')
    text = re.sub(r'(?i)(ticket=)[^&\s\"\'<>]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)Bearer\s+[^\s\"\'<>]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[JWT_REDACTED]', text)
    text = re.sub(r'rediss?://[^\s\"\'<>]+', '[PRIVATE_REDIS]', text)
    return text

class SafeStream:
    def __init__(self, stream): self.stream = stream
    def write(self, text): return self.stream.write(clean(text))
    def flush(self): self.stream.flush()
    def __getattr__(self, name): return getattr(self.stream, name)
sys.stdout, sys.stderr = SafeStream(sys.stdout), SafeStream(sys.stderr)

# Any accidentally unmocked provider call must fail locally, never use live credentials.
original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
def check_address(address):
    if isinstance(address, tuple):
        try:
            allowed = address[0] == 'localhost' or ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            raise OSError('External network disabled in test audit; a mocked or approved staged fixture is required.')
def connect(self, address):
    check_address(address)
    return original_connect(self, address)
def connect_ex(self, address):
    check_address(address)
    return original_connect_ex(self, address)
socket.socket.connect, socket.socket.connect_ex = connect, connect_ex

import django
django.setup()
from django.test.runner import DiscoverRunner

records = []
class Result(unittest.TextTestResult):
    def startTest(self, test):
        self.started = time.perf_counter()
        self.status = 'passed'
        super().startTest(test)
    def addError(self, test, err):
        self.status = 'error'
        super().addError(test, err)
    def addFailure(self, test, err):
        self.status = 'failed'
        super().addFailure(test, err)
    def addSkip(self, test, reason):
        self.status = 'skipped'
        super().addSkip(test, reason)
    def addExpectedFailure(self, test, err):
        self.status = 'expected_failure'
        super().addExpectedFailure(test, err)
    def addUnexpectedSuccess(self, test):
        self.status = 'unexpected_success'
        super().addUnexpectedSuccess(test)
    def addSubTest(self, test, subtest, err):
        if err is not None: self.status = 'failed'
        super().addSubTest(test, subtest, err)
    def stopTest(self, test):
        records.append({'id': test.id(), 'status': self.status,
                        'seconds': round(time.perf_counter()-self.started, 4)})
        super().stopTest(test)

class Runner(DiscoverRunner):
    def get_resultclass(self): return Result
    def run_suite(self, suite, **kwargs):
        self.discovered = suite.countTestCases()
        self.result = super().run_suite(suite, **kwargs)
        return self.result

start = time.perf_counter()
runner = Runner(verbosity=2, interactive=False)
with tempfile.TemporaryDirectory(prefix='full-audit-media-') as media:
    settings.MEDIA_ROOT = media
    failures = runner.run_tests(sys.argv[1:])
result = runner.result
report = {'schema_version': 1, 'scope': sys.argv[1:] or 'all Django default discovery test*.py from repository root',
    'discovered': runner.discovered, 'ran': result.testsRun, 'seconds': round(time.perf_counter()-start, 3),
    'failures': [{'id': t.id(), 'traceback': clean(tb)} for t, tb in result.failures],
    'errors': [{'id': t.id(), 'traceback': clean(tb)} for t, tb in result.errors],
    'skipped': [{'id': t.id(), 'reason': clean(reason)} for t, reason in result.skipped],
    'expected_failures': [t.id() for t, _ in result.expectedFailures],
    'unexpected_successes': [t.id() for t in result.unexpectedSuccesses], 'tests': records,
    'isolation': {'fresh_disposable_database': True, 'legacy_cache': 'isolated LocMemCache to prevent shared cache.clear',
       'explicit_interview_analysis_redis_tests': 'real Redis database15, test-owned keys',
       'broker_default': 'test-owned queue', 'email': 'locmem', 'media': 'temporary directory',
       'external_network': 'non-loopback socket connections denied in runner process'},
    'exit_code': int(bool(failures))}
name = os.environ.get('AUDIT_REPORT_NAME', 'backend')
if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
    raise ValueError('Invalid local audit report name.')
(OUT/(name+'.json')).write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
# Only this audit's own default queue/result keys; never flush/purge a shared service.
from redis import Redis
client = Redis.from_url('redis://127.0.0.1:6379/15', socket_timeout=1)
try:
    keys = list(client.scan_iter(match=prefix+'*'))
    keys += list(client.scan_iter(match='_kombu.binding.'+prefix+'*'))
    if keys: client.delete(*keys)
finally:
    client.close()
print('AUDIT_RESULT '+json.dumps({k:v for k,v in report.items() if k not in ('tests','failures','errors','skipped','isolation')}))
sys.exit(int(bool(failures)))
