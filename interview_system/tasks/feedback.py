from celery import shared_task
from django.db import transaction
from ..feedback import prepare_locked
from ..models import FeedbackReport, Interview


@shared_task(ignore_result=True)
def recover_feedback_reports():
    due = FeedbackReport.objects.filter(status='PENDING', evaluation__status__in=('READY', 'FAILED', 'UNAVAILABLE'))
    for pk in due.order_by('created_at').values_list('pk', flat=True)[:100]:
        ref = FeedbackReport.objects.select_related('evaluation').get(pk=pk)
        with transaction.atomic():
            Interview.objects.select_for_update().get(pk=ref.evaluation.interview_id)
            prepare_locked(ref.evaluation)
