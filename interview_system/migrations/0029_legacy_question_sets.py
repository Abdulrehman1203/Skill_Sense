import hashlib
import json

from django.db import migrations
from django.utils import timezone


def migrate_legacy(apps, schema_editor):
    alias = schema_editor.connection.alias
    Interview = apps.get_model("interview_system", "Interview")
    Question = apps.get_model("interview_system", "Question")
    QuestionSet = apps.get_model("interview_system", "QuestionSet")
    PreparedQuestion = apps.get_model("interview_system", "PreparedQuestion")
    for interview in Interview.objects.using(alias).filter(question_set__isnull=True).iterator():
        questions = list(Question.objects.using(alias).filter(interview=interview).order_by("created_at", "pk"))
        snapshot = {"legacy_interview_id": str(interview.pk), "questions": [
            {"id": str(q.pk), "text": q.text, "category": q.category,
             "source": q.source, "approved": q.approved} for q in questions]}
        digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        now = timezone.now()
        row = QuestionSet.objects.using(alias).create(application_id=interview.application_id,
            input_snapshot=snapshot, input_hash=digest, legacy=True, generation_status="LEGACY",
            frozen_at=now, frozen_revision=1,
            # Do not infer recruiter approval for historical rows.
            fallback_reason="LEGACY_INTERVIEW_IMPORT")
        for i, question in enumerate(questions):
            prepared = PreparedQuestion.objects.using(alias).create(question_set=row,
                text=question.text, category=question.category, source=question.source,
                position=i, archived_at=None if question.approved else now)
            Question.objects.using(alias).filter(pk=question.pk).update(position=i, prepared_question=prepared)
        Interview.objects.using(alias).filter(pk=interview.pk).update(question_set=row)


class Migration(migrations.Migration):
    dependencies = [("interview_system", "0028_preparedquestion_alter_question_options_and_more")]
    operations = [migrations.RunPython(migrate_legacy, migrations.RunPython.noop)]
