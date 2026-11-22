import json

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Min, Q, Sum
from django.utils import timezone

from interview_system.models import InterviewReplay, ReplayBudget, ReplayObject, ReplaySegment
from interview_system.replay import storage


class Command(BaseCommand):
    help = 'Report replay storage and queue metrics, or audit the dedicated R2 prefix.'

    def add_arguments(self, parser):
        parser.add_argument('--inventory', action='store_true')

    def handle(self, *args, **options):
        if options['inventory']:
            self.audit()
        now = timezone.now()
        active = InterviewReplay.objects.filter(state__in=('recording', 'processing'))
        oldest = (active.filter(due_at__lt=now)
                  .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
                  .aggregate(first=Min('due_at'))['first'])
        self.stdout.write(json.dumps({
            'states': dict(InterviewReplay.objects.values('state').annotate(count=Count('pk')).values_list('state', 'count')),
            'reasons': dict(InterviewReplay.objects.exclude(detail='').values('detail').annotate(count=Count('pk')).values_list('detail', 'count')),
            'queued_age_seconds': int((now - oldest).total_seconds()) if oldest else 0,
            'reserved_object_bytes': ReplayObject.objects.aggregate(total=Sum('size'))['total'] or 0,
            'verified_object_bytes': ReplayObject.objects.filter(verified=True).aggregate(total=Sum('size'))['total'] or 0,
            'completed_segments': ReplaySegment.objects.filter(complete=True).count(),
            'incomplete_segments': ReplaySegment.objects.filter(complete=False, ended_ms__isnull=False).count(),
            'unsealed_segments': ReplaySegment.objects.filter(ended_ms__isnull=True).count(),
            'temporary_bytes_reserved': active.aggregate(total=Sum('scratch_bytes'))['total'] or 0,
            'cleanup_backlog': ReplayObject.objects.filter(Q(delete_after__lte=now) | Q(replay__isnull=True)).count(),
            'inventory_ok': ReplayBudget.objects.filter(pk=1, inventory_ok=True).exists(),
        }, sort_keys=True))

    def audit(self):
        if not storage.configured():
            raise CommandError('Configure private replay storage first.')
        unknown = missing = mismatched = 0
        try:
            with storage.budget() as ledger:
                # Pre-existing reservations make concurrent remote writes safe to
                # inspect. Unknown or oversized objects close quota admission.
                expected = {key: (size, verified) for key, size, verified in
                            ReplayObject.objects.values_list('key', 'size', 'verified')}
                s3 = storage.client()
                paginator = s3.get_paginator('list_objects_v2')
                for page in paginator.paginate(Bucket=settings.REPLAY_R2_BUCKET, Prefix=storage.PREFIX):
                    for obj in page.get('Contents', []):
                        reservation = expected.pop(obj['Key'], None)
                        if reservation is None:
                            unknown += 1
                        elif obj['Size'] > reservation[0] or (reservation[1] and obj['Size'] != reservation[0]):
                            mismatched += 1
                missing = sum(verified for _, verified in expected.values())
                ledger.inventory_ok = not (unknown or mismatched or missing)
                ledger.save(update_fields=['inventory_ok'])
        except (BotoCoreError, ClientError, ValueError):
            raise CommandError('Replay storage inventory could not be read.') from None
        self.stdout.write(json.dumps({'untracked': unknown, 'missing': missing, 'size_mismatch': mismatched}))
        if unknown or missing or mismatched:
            raise CommandError('Resolve replay inventory discrepancies before resuming storage writes.')
