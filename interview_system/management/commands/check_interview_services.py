"""Read-only pre-call diagnostics without acquiring devices or creating calls."""
import json

from django.core.management.base import BaseCommand, CommandError

from interview_system.interview_readiness import InterviewNotReady, require_interview_ready


class Command(BaseCommand):
    help = 'Check required video worker and public interviewer callback without creating a provider call.'

    def handle(self, *args, **options):
        try:
            require_interview_ready(video_required=True)
        except InterviewNotReady as error:
            self.stdout.write(json.dumps({'ready': False, 'code': error.code, 'detail': str(error)}))
            raise CommandError('Interview services are not ready.') from None
        self.stdout.write(json.dumps({'ready': True, 'live_audio_verified': False}))
