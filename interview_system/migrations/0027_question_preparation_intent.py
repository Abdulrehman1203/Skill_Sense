from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("interview_system", "0026_analysis_report_index_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="QuestionPreparationIntent",
            fields=[
                ("assessment", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE, primary_key=True,
                    related_name="question_preparation_intent", serialize=False,
                    to="interview_system.applicationassessment",
                )),
                ("screening_event", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="question_preparation_intent",
                    to="interview_system.screeningevent",
                )),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={"ordering": ["created_at", "assessment_id"]},
        ),
    ]
