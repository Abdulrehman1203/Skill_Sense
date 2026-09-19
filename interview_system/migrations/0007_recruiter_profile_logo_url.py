from django.db import migrations, models


def move_external_logo_urls(apps, schema_editor):
    profile_model = apps.get_model("interview_system", "RecruiterProfile")
    for profile in profile_model.objects.filter(company_logo__startswith="http").iterator():
        value = str(profile.company_logo)
        if value.startswith(("https://", "http://")):
            profile.company_logo_url = value
            profile.company_logo = None
            profile.save(update_fields=["company_logo_url", "company_logo"])


class Migration(migrations.Migration):
    dependencies = [("interview_system", "0006_resume_stored_status")]

    operations = [
        migrations.AddField(
            model_name="recruiterprofile",
            name="company_logo_url",
            field=models.URLField(blank=True, default=""),
        ),
        migrations.RunPython(move_external_logo_urls, migrations.RunPython.noop),
    ]
