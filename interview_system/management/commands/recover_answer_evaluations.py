from django.core.management.base import BaseCommand
from interview_system.tasks.evaluation import recover_evaluations


class Command(BaseCommand):
    help = 'Run one bounded transcript/evaluation recovery pass; evaluation dispatch remains durable.'

    def handle(self, *args, **options):
        recover_evaluations.run()
        self.stdout.write('Transcript/evaluation recovery pass completed.')
