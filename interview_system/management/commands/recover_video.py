from django.core.management.base import BaseCommand
from interview_system.video_lifecycle import recover_video_batch


class Command(BaseCommand):
    help = 'Recover due video lifecycles and attempt bounded frame dispatch; broker failure is recoverable.'

    def handle(self, *args, **options):
        count = recover_video_batch()
        self.stdout.write(f'Checked {count} due video sessions.')
