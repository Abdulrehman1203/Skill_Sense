"""Prepare safe projections; retain known historical delivery identities without resending."""
import hashlib
import json
from django.db import migrations


def seed(apps, schema_editor):
    alias = schema_editor.connection.alias
    Evaluation = apps.get_model('interview_system', 'InterviewEvaluation')
    Report = apps.get_model('interview_system', 'FeedbackReport')
    Notification = apps.get_model('interview_system', 'Notification')
    Outbox = apps.get_model('interview_system', 'NotificationOutbox')
    Score = apps.get_model('interview_system', 'ScoreRevision')
    Delivery = apps.get_model('interview_system', 'QuestionReviewDelivery')
    for evaluation in Evaluation.objects.using(alias).all().iterator():
        Report.objects.using(alias).get_or_create(evaluation=evaluation, defaults={'revision': evaluation.revision})
    for notification in Notification.objects.using(alias).filter(type__in=('score_ready', 'question_review_ready')).iterator():
        links = {}
        resource_type = None
        if notification.type == 'score_ready':
            score = Score.objects.using(alias).filter(pk=notification.pk, status='COMPLETE').select_related('evaluation').first()
            if score:
                resource_type, resource_id, revision = 'score_revision', score.pk, str(score.number)
                links = {'application_id': str(score.application_id), 'score_revision_id': str(score.pk)}
                if score.evaluation_id:
                    links['interview_id'] = str(score.evaluation.interview_id)
        else:
            delivery = Delivery.objects.using(alias).filter(pk=notification.pk).select_related('question_set').first()
            if delivery:
                resource_type, resource_id, revision = 'question_set', delivery.question_set_id, f'generation:{delivery.generation_number}'
                links = {'application_id': str(delivery.question_set.application_id), 'question_set_id': str(delivery.question_set_id)}
        if resource_type:
            value = {'recipient': str(notification.user_id), 'event': notification.type, 'resource_type': resource_type,
                     'resource_id': str(resource_id), 'revision': revision}
            key = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            Outbox.objects.using(alias).get_or_create(business_key=key, defaults={'id': notification.pk,
                'recipient_id': notification.user_id, 'event': notification.type, 'resource_type': resource_type,
                'resource_id': resource_id, 'revision': revision, 'links': links,
                'inapp_delivered_at': notification.created_at, 'email_status': 'SKIPPED'})
            Notification.objects.using(alias).filter(pk=notification.pk).update(links=links)


class Migration(migrations.Migration):
    dependencies = [('interview_system', '0038_auditlogentry_business_key_notification_links_and_more')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
