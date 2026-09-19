from django.db import migrations, models
from django.core.validators import FileExtensionValidator
import interview_system.models


class Migration(migrations.Migration):
    dependencies = [
        ("interview_system", "0005_resume_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="resume",
            name="file",
            field=models.FileField(
                upload_to=interview_system.models.resume_upload_path,
                validators=[FileExtensionValidator(allowed_extensions=["pdf", "docx"])],
            ),
        ),
        migrations.AlterField(
            model_name="resume",
            name="status",
            field=models.CharField(
                max_length=10,
                choices=[("STORED", "Stored"), ("PENDING", "Pending"), ("PARSED", "Parsed"), ("FAILED", "Failed")],
                default="PENDING",
                help_text="STORED until submitted; application processing is PENDING → PARSED or FAILED.",
            ),
        ),
    ]
