import uuid

from django.db import migrations, models


SEED_QUESTIONS = (
    ("BEHAVIORAL", "Tell me about a time you received difficult feedback. How did you respond?"),
    ("BEHAVIORAL", "Describe a time you collaborated with someone whose working style differed from yours."),
    ("BEHAVIORAL", "Tell me about a professional mistake and what you learned from it."),
    ("BEHAVIORAL", "Describe a time you had to prioritize several competing responsibilities."),
    ("TECHNICAL", "Walk me through how you diagnose an unfamiliar technical problem."),
    ("TECHNICAL", "How do you verify that a solution is correct, reliable, and maintainable?"),
    ("TECHNICAL", "Describe a technical trade-off you made and how you evaluated the alternatives."),
    ("TECHNICAL", "How do you approach learning a tool or technology that is new to you?"),
    ("SITUATIONAL", "What would you do if a critical deadline were at risk?"),
    ("SITUATIONAL", "How would you respond if requirements changed late in a project?"),
    ("SITUATIONAL", "What would you do if you strongly disagreed with a teammate's proposed approach?"),
    ("SITUATIONAL", "How would you proceed if you lacked important information needed for a decision?"),
)


def seed_template_questions(apps, schema_editor):
    template_model = apps.get_model("interview_system", "TemplateQuestion")
    for index, (category, question_text) in enumerate(SEED_QUESTIONS, start=1):
        # Stable ids make the companion fixture safe to load over migrated seed data.
        question_id = uuid.uuid5(uuid.NAMESPACE_URL, f"skillsense-template-question-{index}")
        template_model.objects.update_or_create(
            pk=question_id,
            defaults={"category": category, "text": question_text},
        )


class Migration(migrations.Migration):
    dependencies = [("interview_system", "0009_scoring_rubric_single_active")]

    operations = [
        migrations.CreateModel(
            name="TemplateQuestion",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("text", models.TextField()),
                (
                    "category",
                    models.CharField(
                        choices=[
                            ("BEHAVIORAL", "Behavioral"),
                            ("TECHNICAL", "Technical"),
                            ("SITUATIONAL", "Situational"),
                        ],
                        max_length=15,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "template question",
                "verbose_name_plural": "template questions",
                "db_table": "template_questions",
                "ordering": ["category", "created_at", "pk"],
            },
        ),
        migrations.AddConstraint(
            model_name="templatequestion",
            constraint=models.UniqueConstraint(
                fields=("text", "category"),
                name="uniq_template_question_text_category",
            ),
        ),
        migrations.RunPython(seed_template_questions, migrations.RunPython.noop),
    ]
