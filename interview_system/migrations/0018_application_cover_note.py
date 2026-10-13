from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("interview_system", "0017_screening_legacy_data"),
    ]

    operations = [
        migrations.AddField(
            model_name="application",
            name="cover_note",
            field=models.TextField(blank=True, default=""),
        ),
    ]
