"""Audited operator recovery and an explicit real-email acceptance probe."""
from django.conf import settings
from django.core.mail import get_connection, send_mail
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.core.validators import validate_email
from django.core.exceptions import ValidationError


class Command(BaseCommand):
    help = 'Inspect/retry a failed outbox delivery, or explicitly send one email acceptance probe.'

    def add_arguments(self, parser):
        parser.add_argument('operation', choices=['status', 'retry', 'test-email'])
        parser.add_argument('--outbox')
        parser.add_argument('--expected-generation', type=int)
        parser.add_argument('--reason')
        parser.add_argument('--to')

    def handle(self, *args, **options):
        import json
        from interview_system.models import NotificationOutbox, NotificationRetry
        if options['operation'] == 'test-email':
            if settings.EMAIL_BACKEND != 'django.core.mail.backends.smtp.EmailBackend':
                raise CommandError('Real email acceptance requires a configured SMTP backend; console/locmem output is not delivery.')
            recipient = options['to']
            try:
                validate_email(recipient or '')
            except ValidationError:
                raise CommandError('Supply the explicitly authorized test recipient with --to.') from None
            try:
                sent = send_mail('SkillSense email delivery verification',
                    'This is the single email delivery verification message you authorized. No candidate information is included.',
                    settings.DEFAULT_FROM_EMAIL, [recipient], connection=get_connection(timeout=10), fail_silently=False)
                if sent != 1:
                    raise RuntimeError()
            except Exception:
                raise CommandError('SMTP acceptance failed; no provider details or credentials logged.') from None
            self.stdout.write(json.dumps({'smtp_accepted': True, 'inbox_receipt_verified': False}))
            return
        if options['operation'] == 'status':
            from django.db.models import Count
            self.stdout.write(json.dumps({'email_backend': settings.EMAIL_BACKEND,
                'by_email_status': dict(NotificationOutbox.objects.values('email_status').annotate(n=Count('pk')).values_list('email_status','n')),
                'due': NotificationOutbox.objects.filter(email_status='PENDING', next_attempt_at__lte=timezone.now()).count()}))
            return
        if not options['outbox'] or not options['expected_generation'] or not (options['reason'] or '').strip():
            raise CommandError('Retry requires --outbox, --expected-generation and --reason.')
        with transaction.atomic():
            try:
                row = NotificationOutbox.objects.select_for_update().get(pk=options['outbox'])
            except (NotificationOutbox.DoesNotExist, ValidationError):
                raise CommandError('Outbox row not found.') from None
            if row.delivery_generation != options['expected_generation']:
                raise CommandError('Delivery generation changed; inspect current state before retrying.')
            if row.email_status != 'FAILED' or (row.lease_until and row.lease_until > timezone.now()):
                raise CommandError('Only terminal failed deliveries with no active lease may be retried.')
            NotificationRetry.objects.create(outbox=row, generation=row.delivery_generation + 1,
                previous_attempts=row.email_attempts, reason=options['reason'])
            row.delivery_generation += 1
            row.email_attempts, row.email_status, row.error_code = 0, 'PENDING', ''
            row.claim_token = row.lease_until = None
            row.next_attempt_at = timezone.now()
            row.save()
            from interview_system.audit import record_audit
            record_audit(actor=None, action='notification.operator_retry', target=row,
                changed_fields=['delivery_generation'], revision=row.delivery_generation)
            from interview_system.notifications import dispatch_notification
            transaction.on_commit(lambda: dispatch_notification(row.pk))
        self.stdout.write(json.dumps({'outbox': str(row.pk), 'delivery_generation': row.delivery_generation, 'status': 'PENDING'}))
