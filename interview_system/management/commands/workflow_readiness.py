"""Read-only rollout diagnostics. Does not dispatch jobs or send notifications."""
import json
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class Command(BaseCommand):
    help = 'Report migration, worker/recovery registration and email rollout readiness without exposing credentials.'

    def add_arguments(self, parser):
        parser.add_argument('--require-ready', action='store_true')
        parser.add_argument('--check-grading', action='store_true', help='Grade a synthetic answer on each configured model.')

    def handle(self, *args, **options):
        executor = MigrationExecutor(connection)
        pending = [m.name for m, backwards in executor.migration_plan(executor.loader.graph.leaf_nodes())]
        from ai_interview_recruitment.celery import app
        required = {'interview_system.tasks.evaluation.recover_evaluations',
            'interview_system.tasks.questions.recover_question_preparation',
            'interview_system.tasks.scoring_tasks.recover_final_scores',
            'interview_system.tasks.notification_tasks.recover_notification_outbox',
            'interview_system.tasks.retell.recover_retell'}
        try:
            inspector = app.control.inspect(timeout=1)
            active = inspector.active() or {}
            reserved = inspector.reserved() or {}
            registered = inspector.registered() or {}
            tasks = set(t for names in registered.values() for t in names)
            required_workers = required | {'interview_system.tasks.conversation.stop_conversation'}
            ready_workers = bool(registered) and not (required_workers - tasks)
            worker_data = {'responding_workers': len(registered), 'active_tasks': sum(len(v) for v in active.values()),
                'reserved_tasks': sum(len(v) for v in reserved.values()), 'missing_recovery_tasks': sorted(required - tasks),
                'ready': ready_workers,
                'conversation_stop_task_registered': 'interview_system.tasks.conversation.stop_conversation' in tasks}
        except Exception:
            worker_data = {'ready': False, 'error_code': 'BROKER_OR_WORKERS_UNAVAILABLE'}
        beat_tasks = {v['task'] for v in settings.CELERY_BEAT_SCHEDULE.values()}
        data = {'pending_migrations': pending, 'workers': worker_data,
            'conversation_guard_configured': bool(settings.RETELL_CONVERSATION_GUARD_ENABLED and settings.RETELL_CONVERSATION_CONTROL_URL),
            'beat_recovery_configured': required.issubset(beat_tasks),
            'email_backend': settings.EMAIL_BACKEND,
            'smtp_configured': settings.EMAIL_BACKEND == 'django.core.mail.backends.smtp.EmailBackend' and settings.EMAIL_HOST != 'localhost',
            'beat_process_verified': False}
        if options['check_grading']:
            data['grading'] = check_grading()
        self.stdout.write(json.dumps(data))
        grading_unavailable = options['check_grading'] and 'ready' not in data['grading'].values()
        if options['require_ready'] and (pending or not worker_data['ready']
                or not data['beat_recovery_configured'] or grading_unavailable):
            raise CommandError('Workflow rollout is not ready. Resolve the reported conditions.')


def check_grading():
    from interview_system.answer_evaluation import InvalidEvaluation, grading_fallback_model
    from interview_system.integrations.gemini_client import (
        EvaluationProviderError, evaluate_answers, has_grading_backup_key,
    )

    question = {'id': 'synthetic', 'text': 'How would you retry a failed job?', 'category': 'TECHNICAL',
                'criteria': {'reasoning': 'Explain the retry and deduplication strategy.'}}
    inputs = {'contract_version': 'answers-v2', 'final_evidence': True, 'context': {'questions': [question]},
              'transcript': {'turns': [{'id': 't1', 'role': 'agent', 'text': question['text']},
                  {'id': 't2', 'role': 'user', 'text': 'Retry transient errors with exponential backoff and jitter. '
                                                    'Use an idempotency key to prevent duplicate side effects.'}]},
              'alignment': {'questions': [{'question_id': 'synthetic', 'state': 'ANSWERED',
                                          'asked_turn_id': 't1', 'answer_turn_ids': ['t2']}]}}
    statuses = {}
    primary = settings.GEMINI_EVALUATION_MODEL_NAME
    fallback = grading_fallback_model(primary)
    routes = [(primary, False)]
    if fallback:
        routes.append((fallback, True))
    for model, is_fallback in routes:
        label = f'{model} (fallback)' if is_fallback and model == primary else model
        try:
            result = evaluate_answers(inputs, model_name=model,
                                      use_fallback_key=is_fallback and has_grading_backup_key())
            statuses[label] = 'ready' if result['answers'][0]['status'] == 'ANSWERED' else 'NO_SCORE'
        except EvaluationProviderError as error:
            statuses[label] = error.code
        except InvalidEvaluation:
            statuses[label] = 'INVALID_OUTPUT'
    return statuses
