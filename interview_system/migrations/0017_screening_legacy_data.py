from django.db import migrations


def migrate_legacy(apps, schema_editor):
    apps.get_model("interview_system", "Job").objects.update(auto_shortlist_enabled=False, non_pass_policy="REVIEW")
    Application = apps.get_model("interview_system", "Application")
    Event = apps.get_model("interview_system", "ScreeningEvent")
    for app in Application.objects.filter(status="SCREENED").iterator():
        Event.objects.create(application_id=app.pk, source="LEGACY", from_status="SCREENED", to_status="SHORTLISTED",
                             reason="Existing manual screening; no automatic qualification inferred.")
    Application.objects.filter(status="SCREENED").update(status="SHORTLISTED")


class Migration(migrations.Migration):
    dependencies = [("interview_system", "0016_application_screening")]
    operations = [migrations.RunPython(migrate_legacy, migrations.RunPython.noop)]
