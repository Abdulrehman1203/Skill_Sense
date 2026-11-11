"""Idempotent explicit initialization; never replaces an existing active policy."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from interview_system.models import ScoringPolicyState, ScoringRubric
from interview_system.final_scoring import activate_rubric


class Command(BaseCommand):
    help = 'Initialize the approved COMBINED_V1 defaults: 40% screening, 60% answers, no visual scoring.'

    @transaction.atomic
    def handle(self, *args, **options):
        ScoringPolicyState.objects.get_or_create(pk=1)
        state = ScoringPolicyState.objects.select_for_update().get(pk=1)
        if state.active_rubric_id:
            self.stdout.write('Existing active scoring policy preserved.')
            return
        if ScoringRubric.objects.filter(active=True).exists():
            raise CommandError('Inconsistent active policy; review before initialization.')
        rubric = ScoringRubric.objects.create(name='Standard screening and interview',
            weight_match=.4, weight_interview=.6, weight_behavioral=0)
        activate_rubric(rubric.pk)
        self.stdout.write(f'Activated COMBINED_V1 version {rubric.version}: 40% screening / 60% answers.')
