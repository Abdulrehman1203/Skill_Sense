"""Remove only expired ephemeral credentials, in bounded batches."""
from django.core.management.base import BaseCommand
from django.utils import timezone
from interview_system.models import SocketTicket


class Command(BaseCommand):
    help = 'Delete up to 1000 expired Interview Analysis socket-ticket digests (run periodically).'

    def handle(self, *args, **options):
        keys = list(SocketTicket.objects.filter(expires_at__lte=timezone.now())
                    .order_by('expires_at').values_list('pk', flat=True)[:1000])
        count, _ = SocketTicket.objects.filter(pk__in=keys).delete()
        self.stdout.write(f'Deleted {count} expired socket tickets.')
