from django.db import migrations, models
from django.db.models import Q


def keep_one_active_rubric(apps, schema_editor):
    """Preserve the oldest active rubric if legacy rows contain several."""
    rubric_model = apps.get_model("interview_system", "ScoringRubric")
    active_ids = list(
        rubric_model.objects.filter(active=True)
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)
    )
    if len(active_ids) > 1:
        rubric_model.objects.filter(pk__in=active_ids[1:]).update(active=False)


class Migration(migrations.Migration):
    dependencies = [("interview_system", "0008_processing_error_webhook_state")]

    operations = [
        migrations.AlterField(
            model_name="scoringrubric",
            name="active",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(keep_one_active_rubric, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="scoringrubric",
            constraint=models.UniqueConstraint(
                fields=["active"],
                condition=Q(active=True),
                name="uniq_active_scoring_rubric",
            ),
        ),
    ]
