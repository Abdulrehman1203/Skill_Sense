from django.core.management.base import BaseCommand
from interview_system.tasks.retell import recover_retell


class Command(BaseCommand):
    help = 'Redispatch pending Retell events and retrieve missed call completions.'

    def handle(self, *args, **options):
        recover_retell()
        self.stdout.write(self.style.SUCCESS('Retell recovery pass completed.'))
