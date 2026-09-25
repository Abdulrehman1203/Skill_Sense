from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('interview_system', '0010_template_question')]
    operations = [migrations.AddField(
        model_name='interviewsession', name='audio_ended_at',
        field=models.DateTimeField(null=True, blank=True,
            help_text='Retell audio completion; ended_at is reserved for video disconnect.'),
    )]
