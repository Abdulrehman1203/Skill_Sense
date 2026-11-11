"""Low-latency ledger dispatch outside ASGI and the inference worker."""
import time
import logging
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone
from interview_system.models import FrameJob
from interview_system.vision_scheduling import dispatch_session


def dispatch_due(limit=100):
    ids = list(FrameJob.objects.filter(state='pending', payload_ready=True,
        publication_due_at__lte=timezone.now()).order_by('received_at')
        .values_list('session_id', flat=True)[:limit])
    for session_id in ids:
        dispatch_session(session_id)
    return len(ids)


class Command(BaseCommand):
    help = 'Dispatch committed video frame references every 250ms; recovery remains the durable fallback.'

    def handle(self, *args, **options):
        while True:
            try:
                close_old_connections()
                dispatch_due()
            except KeyboardInterrupt:
                return
            except Exception as exc:
                logging.getLogger(__name__).warning('vision_dispatch_failed error_type=%s', type(exc).__name__)
                time.sleep(1)
            finally:
                close_old_connections()
            time.sleep(.25)
