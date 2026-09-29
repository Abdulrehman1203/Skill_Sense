from django.db import migrations, models
from django.db.models.functions import Lower, Trim


class Migration(migrations.Migration):
    dependencies = [("interview_system", "0011_interviewsession_audio_ended_at")]
    operations = [
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                Lower(Trim("email")), name="users_email_normalized_unique"
            ),
        ),
    ]
