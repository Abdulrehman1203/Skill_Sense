from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('interview_system', '0049_resume_processing_code')]

    operations = [
        migrations.AddField(
            model_name='notificationoutbox',
            name='send_in_app',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='interview',
            name='deadline_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='interview',
            name='notify_in_app',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='interview',
            name='notify_email',
            field=models.BooleanField(default=True),
        ),
        migrations.AddConstraint(
            model_name='interview',
            constraint=models.CheckConstraint(
                condition=models.Q(deadline_at__isnull=True) | models.Q(scheduled_at__isnull=True),
                name='interview_single_timing_type',
            ),
        ),
        migrations.AddConstraint(
            model_name='interview',
            constraint=models.CheckConstraint(
                condition=models.Q(notify_in_app=True) | models.Q(notify_email=True),
                name='interview_delivery_channel_required',
            ),
        ),
    ]
