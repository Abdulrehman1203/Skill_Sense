from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from interview_system.models import NotificationOutbox
from interview_system.tasks.feedback import recover_feedback_reports
from interview_system.tasks.notification_tasks import deliver_notification_outbox, recover_notification_outbox


class Command(BaseCommand):
    help = 'One bounded feedback projection/outbox recovery pass; optional inline delivery sends email.'

    def add_arguments(self, parser):
        parser.add_argument('--inline-delivery', action='store_true')

    def handle(self, *args, **options):
        recover_feedback_reports.run()
        if options['inline_delivery']:
            due = NotificationOutbox.objects.filter(Q(inapp_delivered_at__isnull=True) | Q(
                email_status__in=('PENDING', 'SENDING'), next_attempt_at__lte=timezone.now())).filter(
                Q(lease_until__isnull=True) | Q(lease_until__lte=timezone.now()))
            for pk in list(due.order_by('next_attempt_at').values_list('pk', flat=True)[:100]):
                deliver_notification_outbox.run(str(pk))
        else:
            recover_notification_outbox.run()
        self.stdout.write('Bounded feedback/notification recovery completed.')
