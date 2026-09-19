from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("interview_system", "0007_recruiter_profile_logo_url")]

    operations = [
        migrations.AddField(
            model_name="resume",
            name="processing_error",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.CreateModel(
            name="ClerkWebhookState",
            fields=[
                ("clerk_id", models.CharField(max_length=255, primary_key=True, serialize=False)),
                ("last_event_at", models.DateTimeField()),
                ("is_deleted", models.BooleanField(default=False)),
            ],
            options={"db_table": "clerk_webhook_states"},
        ),
    ]
