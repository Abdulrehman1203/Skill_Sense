from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("interview_system", "0057_interview_evaluation_fallback_route")]

    operations = [
        migrations.CreateModel(
            name="RecruiterCalendarConnection",
            fields=[
                ("recruiter", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    primary_key=True,
                    related_name="calendar_connection",
                    serialize=False,
                    to="interview_system.recruiterprofile",
                )),
                ("state_digest", models.CharField(editable=False, max_length=64, null=True, unique=True)),
                ("state_expires_at", models.DateTimeField(null=True)),
                ("encrypted_refresh_token", models.BinaryField(editable=False, null=True)),
                ("connected_at", models.DateTimeField(null=True)),
            ],
        ),
    ]
