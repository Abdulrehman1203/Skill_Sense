from django.core.management.base import BaseCommand
from interview_system.models import Application
from interview_system.screening import create_assessment


class Command(BaseCommand):
    help = "Create missing application assessments. Dry-run by default; bounded and resumable."

    def add_arguments(self, parser):
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--job")
        parser.add_argument("--apply-decisions", action="store_true", help="Allow policy transitions for pending/review applications.")

    def handle(self, *args, **options):
        qs = Application.objects.filter(current_assessment__isnull=True).order_by("created_at", "pk")
        if options["job"]:
            qs = qs.filter(job_id=options["job"])
        count = 0
        for app in qs[:max(0, min(options["limit"], 1000))]:
            self.stdout.write(f"{app.pk}: {app.status}")
            if options["execute"]:
                create_assessment(app.pk, apply_decision=options["apply_decisions"] and app.status in ("APPLIED", "UNDER_REVIEW"))
            count += 1
        self.stdout.write(f"{'Queued' if options['execute'] else 'Would queue'} {count} assessments.")
