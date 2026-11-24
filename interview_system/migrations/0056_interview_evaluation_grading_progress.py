from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('interview_system', '0055_browser_observations')]
    operations = [
        migrations.AddField(
            model_name='interviewevaluation',
            name='grading_progress',
            field=models.JSONField(blank=True, default=dict, db_default={}),
        ),
    ]
