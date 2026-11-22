from django.conf import settings
from django.core.management.base import BaseCommand
from redis import Redis


class Command(BaseCommand):
    help = 'Set the live monitor admission mode on all signaling instances.'

    def add_arguments(self, parser):
        parser.add_argument('mode', choices=('open', 'drain', 'off'))

    def handle(self, *args, mode, **options):
        with Redis.from_url(settings.INTERVIEW_ANALYSIS_REDIS_URL,
                            socket_timeout=2, socket_connect_timeout=2) as client:
            client.set(settings.INTERVIEW_ANALYSIS_REDIS_PREFIX + ':monitor:mode', mode)
        self.stdout.write(f'Live monitor mode set to {mode}.')
