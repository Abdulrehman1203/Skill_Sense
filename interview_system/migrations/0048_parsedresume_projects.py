from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("interview_system", "0047_candidateprofile_github_url")]

    operations = [
        migrations.AddField(
            model_name="parsedresume",
            name="projects",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
