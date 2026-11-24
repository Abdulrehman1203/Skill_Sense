from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('interview_system', '0054_gaze_calibration_context')]
    operations = [migrations.AddField(
        model_name='interviewsession', name='browser_observations',
        field=models.JSONField(default=dict, blank=True, db_default={}),
    )]
