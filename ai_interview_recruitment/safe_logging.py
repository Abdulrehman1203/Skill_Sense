"""Production logs are allowlisted events, never arbitrary message/exception data."""
import json
import logging
import re
from contextvars import ContextVar

request_id = ContextVar('request_id', default='')
EVENTS = {'http_completed', 'task_completed', 'task_failed', 'dispatch_deferred', 'recovery_scan', 'cache_observation'}
FIELDS = {'event_code','request_id','task_id','component','route','method','status','duration_ms','count','cache_kind','cache_outcome'}
_STANDARD = set(logging.makeLogRecord({}).__dict__)


class CredentialFilter(logging.Filter):
    def __init__(self, name='', strict=False):
        super().__init__(name)
        self.strict = strict

    def filter(self, record):
        if not getattr(record, 'request_id', None):
            record.request_id = request_id.get()
        if self.strict:
            # Never even format untrusted args: a formatting failure can make
            # logging's fallback print the original message/arguments to stderr.
            record.msg = record.event_code if getattr(record, 'event_code', '') in EVENTS else 'runtime_log'
            record.args = ()
            for key in list(record.__dict__):
                if key not in _STANDARD and key not in FIELDS:
                    del record.__dict__[key]
            record.exc_info = record.exc_text = record.stack_info = None
            return True
        message = record.getMessage()
        message = re.sub(r'(?i)(ticket=|token=|api_key=|signature=)[^&\s\"\'<>]+', r'\1[REDACTED]', message)
        message = re.sub(r'(?i)Bearer\s+[^\s\"\'<>]+', 'Bearer [REDACTED]', message)
        message = re.sub(r'[A-Za-z0-9_.:/-]+\?[^\s]+', '[PRIVATE_QUERY]', message)
        message = re.sub(r'rediss?://[^\s\"\'<>]+', '[PRIVATE_REDIS]', message)
        message = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[PRIVATE_JWT]', message)
        message = re.sub(r'[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[PRIVATE_EMAIL]', message)
        message = re.sub(r'\buser_[A-Za-z0-9_-]+', '[PRIVATE_ID]', message)
        message = re.sub(r'(?i)(resume_text|transcript|request_body|body)\s*[=:].*', r'\1=[REDACTED]', message, flags=re.S)
        record.msg, record.args = message, ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


class EventFormatter(logging.Formatter):
    def format(self, record):
        # Deliberately ignore free-form message, exception, arbitrary extras and
        # task results. Regex alone cannot reliably recognize resume/transcript PII.
        event = getattr(record, 'event_code', '')
        source = ('django' if record.name.startswith('django') else 'celery' if record.name.startswith('celery')
                  else 'application' if record.name.startswith(('operations','interview_system','ai')) else 'external')
        row = {'timestamp': round(record.created, 3), 'level': record.levelname, 'source': source,
               'event': event if event in EVENTS else 'runtime_log'}
        for key in ('request_id','task_id'):
            value = str(getattr(record, key, ''))
            if re.fullmatch(r'[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value):
                row[key] = value
        for key, allowed in {'component': {'http','screening','notification','other'},
                             'cache_kind': {'embedding','gemini_parse'},
                             'cache_outcome': {'hit','miss','stored'},
                             'method': {'GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS','OTHER'}}.items():
            value = getattr(record, key, '')
            if value in allowed:
                row[key] = value
        route = getattr(record, 'route', '')
        if route in {'resume','application','health','metrics','auth','other','unmatched'}:
            row['route'] = route
        for key in ('status','duration_ms','count'):
            value = getattr(record, key, None)
            if type(value) in (int, float) and 0 <= value < 1e12:
                row[key] = round(value, 3)
        return json.dumps(row, allow_nan=False)


def emit_event(code, **values):
    if code not in EVENTS:
        raise ValueError('Unregistered operational event')
    values = {key: value for key, value in values.items() if key in FIELDS}
    values.update(event_code=code, request_id=request_id.get())
    logger = logging.getLogger('operations')
    write = logger.warning if code in {'task_failed','dispatch_deferred'} else logger.info
    write(code, extra=values)


def production_logging():
    return {'version': 1, 'disable_existing_loggers': True,
        'filters': {'credentials': {'()': 'ai_interview_recruitment.safe_logging.CredentialFilter', 'strict': True}},
        'formatters': {'safe': {'()': 'ai_interview_recruitment.safe_logging.EventFormatter'}},
        'handlers': {'console': {'class': 'logging.StreamHandler', 'filters': ['credentials'], 'formatter': 'safe'}},
        'root': {'handlers': ['console'], 'level': 'INFO'},
        'loggers': {name: {'handlers': ['console'], 'level': 'INFO' if name in ('operations', 'ai.matching.cache') else 'WARNING', 'propagate': False}
                    for name in ('operations','django','django.request','django.server','daphne','celery','celery.task','httpx','httpcore',
                                 'interview_system','ai_interview_recruitment','ai','google','urllib3','botocore','ai.matching.cache')}}
