import json
from django.core.management.base import BaseCommand, CommandError
from interview_system.behavioral_analysis_operations import health, metrics, retention_plan


class Command(BaseCommand):
    help = 'Local, read-only Interview Analysis health/metrics or explicit derived-retention inventory. No secrets or IDs.'

    def add_arguments(self, parser):
        parser.add_argument('mode', choices=('health', 'metrics', 'retention-plan'))
        parser.add_argument('--days', type=int)
        parser.add_argument('--require-ready', action='store_true')

    def handle(self, *args, **options):
        if options['mode'] == 'retention-plan' and not (options['days'] and 1 <= options['days'] <= 36500):
            raise CommandError('retention-plan requires --days in 1..36500; it never deletes data.')
        try:
            result = (health() if options['mode'] == 'health' else metrics()
                      if options['mode'] == 'metrics' else retention_plan(options['days']))
        except Exception:
            raise CommandError('Interview Analysis operator read unavailable; check migrations/connectivity privately.') from None
        self.stdout.write(json.dumps(result, allow_nan=False, sort_keys=True))
        if options['require_ready'] and (options['mode'] != 'health' or not result['ready']
                                        or not result['payload_policy_compliant']):
            raise CommandError('Interview Analysis readiness or private payload policy is not satisfied.')
