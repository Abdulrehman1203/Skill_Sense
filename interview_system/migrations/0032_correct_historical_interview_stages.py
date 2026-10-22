from django.db import migrations


def correct_stages(apps, schema_editor):
    alias = schema_editor.connection.alias
    Interview = apps.get_model("interview_system", "Interview")
    Application = apps.get_model("interview_system", "Application")
    Event = apps.get_model("interview_system", "ScreeningEvent")
    # Do not infer successful completion/evaluation or actors for historical data.
    Interview.objects.using(alias).filter(status="DONE").update(
        completion_outcome="LEGACY_UNVERIFIED", evaluation_status="UNAVAILABLE")
    for app in Application.objects.using(alias).filter(status="INTERVIEWED").iterator():
        latest = Interview.objects.using(alias).filter(application_id=app.pk).order_by("-created_at", "-pk").first()
        target = {"SCHEDULED": "SCHEDULED", "LIVE": "IN_PROGRESS"}.get(latest.status if latest else "")
        if target:
            Application.objects.using(alias).filter(pk=app.pk).update(status=target, version=app.version + 1)
            Event.objects.using(alias).create(application_id=app.pk, assessment_id=app.current_assessment_id,
                source="SYSTEM", from_status="INTERVIEWED", to_status=target,
                reason="Historical stage corrected from persisted interview lifecycle; no completion or evaluation inferred.")


class Migration(migrations.Migration):
    dependencies = [("interview_system", "0031_interview_completed_at_interview_completion_outcome_and_more")]
    operations = [migrations.RunPython(correct_stages, migrations.RunPython.noop)]
