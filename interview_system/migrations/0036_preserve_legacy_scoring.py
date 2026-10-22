"""Preserve old scores; activate safe weights only when scoring was configured."""
import hashlib
from decimal import Decimal, ROUND_HALF_UP
from django.db import migrations


def preserve(apps, schema_editor):
    alias = schema_editor.connection.alias
    Rubric = apps.get_model('interview_system', 'ScoringRubric')
    State = apps.get_model('interview_system', 'ScoringPolicyState')
    Revision = apps.get_model('interview_system', 'ScoreRevision')
    Score = apps.get_model('interview_system', 'CandidateScore')
    configured = Rubric.objects.using(alias).filter(active=True).exists()
    for number, rubric in enumerate(Rubric.objects.using(alias).order_by('created_at', 'pk'), 1):
        Rubric.objects.using(alias).filter(pk=rubric.pk).update(version=number, policy_version='LEGACY_V0', active=False)
    next_version = Rubric.objects.using(alias).count() + 1
    active = None
    if configured:
        active = Rubric.objects.using(alias).create(name='Screening and evaluated answers v1', version=next_version,
            policy_version='COMBINED_V1', weight_match=.4, weight_interview=.6, weight_behavioral=0, active=True)
        next_version += 1
    State.objects.using(alias).update_or_create(pk=1, defaults={'active_rubric': active,
        'next_version': next_version, 'rebuild_pending': True, 'rebuild_cursor': None, 'epoch': 1})
    for score in Score.objects.using(alias).all().iterator():
        value = Decimal(str(score.final_score)) if score.final_score is not None else None
        value = value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP) if value is not None and value.is_finite() and 0 <= value <= 100 else None
        revision = Revision.objects.using(alias).create(application_id=score.application_id, number=1,
            key=hashlib.sha256(f'legacy-score:{score.pk}'.encode()).hexdigest(), status='LEGACY',
            inputs={'policy': 'LEGACY_V0', 'original_score_id': str(score.pk), 'breakdown': score.breakdown,
                    'original_row_created_at': score.created_at.isoformat(),
                    'explanation': score.explanation, 'provenance': 'UNVERIFIED_LEGACY'},
            final_score=value)
        Score.objects.using(alias).filter(pk=score.pk).update(current_revision=revision)


class Migration(migrations.Migration):
    dependencies = [('interview_system', '0035_candidatescore_updated_at_and_more')]
    operations = [migrations.RunPython(preserve, migrations.RunPython.noop)]
