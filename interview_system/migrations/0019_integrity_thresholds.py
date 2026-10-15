from django.db import migrations, models


def seed_defaults(apps, schema_editor):
    apps.get_model("interview_system", "IntegrityThresholds").objects.using(schema_editor.connection.alias).get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [("interview_system", "0018_application_cover_note")]
    operations = [
        migrations.CreateModel(
            name="IntegrityThresholds",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("look_away_limit", models.PositiveIntegerField(default=5)),
                ("integrity_flag_limit", models.PositiveIntegerField(default=1)),
            ],
            options={"db_table": "integrity_thresholds", "constraints": [
                models.CheckConstraint(condition=models.Q(id=1), name="integrity_thresholds_singleton"),
            ]},
        ),
        migrations.RunPython(seed_defaults, migrations.RunPython.noop),
    ]
