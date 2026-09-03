from django.apps import AppConfig


class InterviewSystemConfig(AppConfig):
    name = "interview_system"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        # Import signals module so receivers are registered.
        # This is the recommended Django pattern — do NOT rely on
        # import side-effects in models.py.
        import interview_system.signals  # noqa: F401
