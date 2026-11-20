from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("interview_system", "0048_parsedresume_projects")]

    operations = [
        migrations.AddField(
            model_name="resume",
            name="processing_code",
            field=models.CharField(
                blank=True,
                choices=[
                    ("RETRY_PENDING", "Automatic retry pending"),
                    ("RETRY_AVAILABLE", "Manual retry available"),
                    ("UNREADABLE", "Unreadable document"),
                    ("SUPPORT_REQUIRED", "Support required"),
                ],
                default="",
                db_default="",
                max_length=20,
            ),
        ),
    ]
