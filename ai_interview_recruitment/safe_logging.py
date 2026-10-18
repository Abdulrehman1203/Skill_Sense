"""Defense in depth; never enable request-body/header/ASGI-wire logging."""
import logging
import re


class CredentialFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        message = re.sub(r'(?i)(ticket=)[^&\s\"\'<>]+', r'\1[REDACTED]', message)
        message = re.sub(r'(?i)Bearer\s+[^\s\"\'<>]+', 'Bearer [REDACTED]', message)
        message = re.sub(r'rediss?://[^\s\"\'<>]+', '[PRIVATE_REDIS]', message)
        record.msg, record.args = message, ()
        # Tracebacks/exception messages may embed provider URLs/credentials.
        # Debugging needs a separately sanitized reproduction, never raw logs.
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True
