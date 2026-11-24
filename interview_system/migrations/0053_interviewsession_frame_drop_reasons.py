from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('interview_system', '0052_interview_replay')]

    operations = [
        migrations.AddField(
            model_name='interviewsession',
            name='frame_drop_reasons',
            field=models.JSONField(default=dict, blank=True, db_default={}),
        ),
    ]
