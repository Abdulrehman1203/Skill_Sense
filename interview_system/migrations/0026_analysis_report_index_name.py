from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('interview_system', '0025_phase8_report_delivery'),
    ]

    operations = [
        migrations.RenameIndex(
            model_name='interviewsession',
            old_name='phase8_report_due',
            new_name='analysis_report_due',
        ),
    ]
