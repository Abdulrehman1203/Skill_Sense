from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("interview_system", "0058_recruiter_calendar_connection")]

    operations = [
        migrations.AddField(
            model_name="resume",
            name="upload_hash",
            field=models.CharField(blank=True, default="", editable=False, max_length=64),
        ),
    ]
