from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.db import transaction

from interview_system.answer_evaluation import grading_fallback_model, prepare_evaluation_locked
from interview_system.models import Interview, TranscriptPreparation
from interview_system.tasks.evaluation import evaluate_interview, recover_evaluations


class Command(BaseCommand):
    help = 'Run one bounded transcript/evaluation recovery pass; evaluation dispatch remains durable.'

    def add_arguments(self, parser):
        from uuid import UUID
        parser.add_argument('--retry-provider-failure', type=UUID, metavar='INTERVIEW_ID')
        parser.add_argument('--run-inline', action='store_true', help='Claim the targeted revision in this process.')
        parser.add_argument('--use-fallback', action='store_true', help='Use the configured fallback model for the targeted revision.')

    def handle(self, *args, **options):
        if options['retry_provider_failure']:
            model = grading_fallback_model(settings.GEMINI_EVALUATION_MODEL_NAME) if options['use_fallback'] else None
            if options['use_fallback'] and not model:
                raise CommandError('Configure a separate grading fallback key or model first.')
            with transaction.atomic():
                interview = Interview.objects.select_for_update().filter(pk=options['retry_provider_failure']).first()
                if interview is None or interview.status != 'DONE' or interview.completion_outcome != 'USABLE':
                    raise CommandError('A completed interview with verified usable evidence is required.')
                latest = interview.evaluations.order_by('-revision').first()
                prep = TranscriptPreparation.objects.filter(interview=interview, state='READY').first()
                if not latest or not prep or prep.selected_id != latest.transcript_id:
                    raise CommandError('The latest evaluation and the selected transcript must agree.')
                if latest.status != 'FAILED' or not latest.error_code.startswith('PROVIDER_'):
                    raise CommandError('The latest evaluation must have a terminal grading provider error.')
                evaluation = prepare_evaluation_locked(interview, prep, rerun=True,
                    reason='Operator recovery of a terminal grading provider failure.',
                    model_name=model, dispatch=not options['run_inline'], uses_fallback=options['use_fallback'])
            if options['run_inline']:
                evaluate_interview.run(str(evaluation.pk))
                evaluation.refresh_from_db()
            self.stdout.write(f'Evaluation revision {evaluation.revision} is {evaluation.status} ({evaluation.pk}).')
            return
        if options['run_inline'] or options['use_fallback']:
            raise CommandError('--run-inline and --use-fallback require --retry-provider-failure.')
        recover_evaluations.run()
        self.stdout.write('Transcript/evaluation recovery pass completed.')
