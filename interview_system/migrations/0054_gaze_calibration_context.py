from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('interview_system', '0053_interviewsession_frame_drop_reasons')]

    operations = [
        migrations.AddField(
            model_name='interviewsession', name='gaze_setup',
            field=models.JSONField(blank=True, default=dict, db_default={}),
        ),
        migrations.AddField(
            model_name='framejob', name='vision_context',
            field=models.JSONField(blank=True, default=dict, db_default={}),
        ),
    ]
