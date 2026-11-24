from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('interview_system', '0056_interview_evaluation_grading_progress')]
    operations = [
        migrations.AddField(
            model_name='interviewevaluation', name='uses_fallback',
            field=models.BooleanField(default=False, db_default=False),
        ),
        migrations.AddField(
            model_name='interviewevaluation', name='provider_failures',
            field=models.PositiveIntegerField(default=0, db_default=0),
        ),
    ]
