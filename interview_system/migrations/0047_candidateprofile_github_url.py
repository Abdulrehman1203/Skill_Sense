from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("interview_system", "0046_continuation_evidence"),
    ]

    operations = [
        migrations.AddField(
            model_name="candidateprofile",
            name="github_url",
            field=models.URLField(blank=True, default=""),
        ),
    ]
