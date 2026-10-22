"""Compare a consenting recruiter's labelled examples without changing application data."""
import json
import math
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from interview_system.answer_evaluation import validate_evaluation
from interview_system.integrations.gemini_client import evaluate_answers


class Command(BaseCommand):
    help = 'Evaluate a local recruiter-labelled benchmark. Sends its textual evidence to configured Gemini.'

    def add_arguments(self, parser):
        parser.add_argument('file')
        parser.add_argument('--max-mae', type=float, default=15)

    def handle(self, *args, **options):
        try:
            data = json.loads(Path(options['file']).read_text(encoding='utf-8'))
            if data['provenance'] != 'RECRUITER_LABELLED' or not data['reviewer'].strip():
                raise ValueError('A real recruiter reviewer and labelled provenance are required.')
            cases = data['cases']
            if not isinstance(cases, list) or not 6 <= len(cases) <= 30:
                raise ValueError('Supply 6 to 30 reviewed cases across all three question categories.')
            categories = {q['category'] for c in cases for q in c['input']['context']['questions']}
            if categories != {'TECHNICAL', 'BEHAVIORAL', 'SITUATIONAL'}:
                raise ValueError('All three question categories are required.')
            for case in cases:
                if type(case['expected_score']) not in (int, float) or not math.isfinite(case['expected_score']) or not 0 <= case['expected_score'] <= 100:
                    raise ValueError('Expected scores must be finite in 0..100.')
            if not math.isfinite(options['max_mae']) or options['max_mae'] < 0:
                raise ValueError('Invalid maximum MAE.')
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CommandError(f'Invalid benchmark: {exc}') from None
        results, errors = [], []
        model = getattr(settings, 'GEMINI_EVALUATION_MODEL_NAME', settings.GEMINI_MODEL_NAME)
        for index, case in enumerate(cases):
            try:
                answers, score, coverage = validate_evaluation(evaluate_answers(case['input'], model_name=model), case['input'])
                if score is None or coverage != 1:
                    raise ValueError('Missing evidence')
                error = abs(score - case['expected_score'])
                errors.append(error)
                results.append({'case': index + 1, 'score': score, 'absolute_error': error, 'answers': answers})
            except Exception:
                results.append({'case': index + 1, 'error': 'EVALUATION_FAILED'})
        mae = sum(errors) / len(errors) if errors else None
        passed = len(errors) == len(cases) and mae <= options['max_mae']
        self.stdout.write(json.dumps({'contract': 'answers-v1', 'model': model, 'cases': results,
            'mae': mae, 'passed': passed}, allow_nan=False))
        if not passed:
            raise CommandError('Benchmark requires recruiter review; quality threshold not met.')
