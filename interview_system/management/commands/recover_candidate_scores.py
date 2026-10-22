from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from interview_system.models import ScoreRevision
from interview_system.tasks.scoring_tasks import calculate_score_revision, recover_final_scores


class Command(BaseCommand):
    help = 'Run one bounded scoring recovery/rebuild pass; optionally compute due jobs without a broker.'

    def add_arguments(self, parser):
        parser.add_argument('--inline', action='store_true')

    def handle(self, *args, **options):
        recover_final_scores.run()
        if options['inline']:
            due = ScoreRevision.objects.filter(status__in=('QUEUED', 'PROCESSING'), next_attempt_at__lte=timezone.now()).filter(
                Q(lease_until__isnull=True) | Q(lease_until__lte=timezone.now()))
            for pk in list(due.order_by('next_attempt_at').values_list('pk', flat=True)[:100]):
                calculate_score_revision.run(str(pk))
        self.stdout.write('Bounded scoring recovery pass completed.')
