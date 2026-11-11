"""Private Prometheus snapshot: aggregates only, no entity or credential labels."""
import secrets
from datetime import timedelta
from django.conf import settings
from django.db.models import Min
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from ai_interview_recruitment.operations import http_metrics
from .models import ApplicationAssessment, NotificationOutbox, Resume, TranscriptRevision


@require_GET
def metrics(request):
    expected = getattr(settings, 'METRICS_TOKEN', '')
    supplied = request.headers.get('Authorization', '')
    if not expected or not secrets.compare_digest(supplied.encode('utf-8'), ('Bearer ' + expected).encode('utf-8')):
        return JsonResponse({'detail': 'Unauthorized.'}, status=401)
    now = timezone.now()
    try:
        active = ApplicationAssessment.objects.filter(status__in=('QUEUED','PARSING','MATCHING','SCREENING'))
        oldest = active.aggregate(value=Min('next_attempt_at'))['value']
        values = {
            'screening_pending': active.count(),
            'screening_failed': ApplicationAssessment.objects.filter(status='FAILED').count(),
            'screening_expired_leases': active.filter(lease_until__lt=now).count(),
            'resume_expired_leases': Resume.objects.filter(parse_lease_until__lt=now).count(),
            'notification_pending': NotificationOutbox.objects.filter(email_status__in=('PENDING','SENDING')).count(),
            'notification_failed': NotificationOutbox.objects.filter(email_status='FAILED').count(),
            'screening_oldest_due_age_seconds': max(0., (now - oldest).total_seconds()) if oldest else 0.,
            'resumes_retention_review_due': Resume.objects.filter(
                uploaded_at__lt=now - timedelta(days=getattr(settings, 'RETENTION_RESUME_DAYS', 90))).count(),
            'transcripts_retention_review_due': TranscriptRevision.objects.filter(
                created_at__lt=now - timedelta(days=getattr(settings, 'RETENTION_TRANSCRIPT_DAYS', 90))).count(),
        }
    except Exception:
        return JsonResponse({'status': 'unavailable'}, status=503)
    lines = []
    for key, value in values.items():
        lines += [f'# TYPE skillsense_{key} gauge', f'skillsense_{key} {value}']
    for route, item in http_metrics().items():
        for metric, value in item.items():
            lines.append(f'skillsense_http_{metric}_total{{route="{route}"}} {value}')
    response = HttpResponse('\n'.join(lines) + '\n', content_type='text/plain; version=0.0.4')
    response['Cache-Control'] = 'no-store'
    return response
