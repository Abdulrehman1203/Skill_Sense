"""Leased question generation with durable dispatch recovery and no draft overwrite."""
from datetime import timedelta
import uuid

from celery import shared_task
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from ..models import Application, Notification, PreparedQuestion, QuestionPreparationIntent, QuestionReviewDelivery, QuestionSet
from ..question_preparation import dispatch, generation_context, prepare_locked


@shared_task(ignore_result=True, acks_late=True, soft_time_limit=50, time_limit=60)
def generate_question_set(set_id):
    token = uuid.uuid4()
    with transaction.atomic():
        row = QuestionSet.objects.select_for_update().get(pk=set_id)
        if row.generation_status not in ("QUEUED", "GENERATING") or row.next_attempt_at > timezone.now():
            return
        if row.lease_until and row.lease_until > timezone.now():
            return
        if (row.items.filter(archived_at__isnull=True, recruiter_edited=False).exclude(source="MANUAL").exists()
                or row.frozen_at or row.approved_at):
            return  # Never overwrite recruiter or legacy content.
        if row.attempts >= 4:
            row.generation_status, row.error_code = "FAILED", "RECOVERY_EXHAUSTED"
            row.claim_token = row.lease_until = None
            row.save()
            return
        row.claim_token, row.lease_until = token, timezone.now() + timedelta(minutes=2)
        row.generation_status, row.attempts = "GENERATING", row.attempts + 1
        row.save()
    try:
        from ..integrations.gemini_client import generate_questions_for, get_template_questions
        job, candidate = generation_context(row)
        fallback_reason = ""
        try:
            questions = generate_questions_for(job, candidate)
            fallback_reason = getattr(questions, 'fallback_reason', '')
        except Exception:
            questions = get_template_questions()
            fallback_reason = "GENERATOR_UNAVAILABLE"
        if not questions:
            questions = get_template_questions()
            fallback_reason = "EMPTY_GENERATION"
        if not 1 <= len(questions) <= 100:
            raise ValueError("Invalid question count")
        for q in questions:
            if (not isinstance(q.get("text"), str) or not q["text"].strip()
                    or q.get("category") not in ("BEHAVIORAL", "TECHNICAL", "SITUATIONAL")
                    or q.get("source") not in ("GENERATED", "TEMPLATE")):
                raise ValueError("Invalid question output")
        fallback = any(q["source"] == "TEMPLATE" for q in questions)
        with transaction.atomic():
            current = QuestionSet.objects.select_for_update().get(pk=set_id)
            if current.claim_token != token or current.revision != row.revision or current.approved_at or current.frozen_at:
                return
            preserved = list(current.items.filter(archived_at__isnull=True))
            questions = questions[:max(0, 100 - len(preserved))]
            for position, question in enumerate(preserved):
                question.position = position
            PreparedQuestion.objects.bulk_update(preserved, ["position"])
            PreparedQuestion.objects.bulk_create([PreparedQuestion(question_set=current,
                text=q["text"].strip(), category=q["category"], source=q["source"],
                position=i + len(preserved), generation_number=current.generation_number)
                for i, q in enumerate(questions)])
            current.generation_status = "FALLBACK" if fallback else "READY"
            current.fallback_reason = (fallback_reason or "GENERATOR_TEMPLATE_FALLBACK") if fallback else ""
            current.error_code = ""
            current.claim_token = current.lease_until = None
            current.revision += 1
            current.save()
            delivery, _ = QuestionReviewDelivery.objects.get_or_create(
                question_set=current, generation_number=current.generation_number)
            from ..notifications import queue_notification
            queue_notification(current.application.job.recruiter.user_id, 'question_review_ready',
                resource_type='question_set', resource_id=current.pk, revision=f'generation:{current.generation_number}',
                links={'application_id': current.application_id}, notification_id=delivery.pk)
            transaction.on_commit(lambda: dispatch_review_delivery(delivery.pk))
    except Exception:
        QuestionSet.objects.filter(pk=set_id, claim_token=token).update(
            generation_status="FAILED" if row.attempts >= 4 else "QUEUED",
            error_code="GENERATION_FAILED", claim_token=None, lease_until=None,
            next_attempt_at=timezone.now() + timedelta(seconds=min(300, 15 * 2 ** row.attempts)))


@shared_task(ignore_result=True)
def recover_question_preparation():
    # Recover G01 intent rows created before this consumer was deployed.
    intents = QuestionPreparationIntent.objects.filter(assessment__question_set__isnull=True,
        assessment__status="SUCCEEDED",
        assessment__resume_id=F("assessment__application__resume_id"),
        assessment__application__current_assessment_id=F('assessment_id'),
        assessment__application__status="SHORTLISTED").order_by("created_at")[:100]
    for intent in intents:
        with transaction.atomic():
            app = Application.objects.select_for_update().get(pk=intent.assessment.application_id)
            if app.status == "SHORTLISTED" and app.current_assessment_id == intent.assessment_id:
                prepare_locked(app, intent.assessment)
    due = QuestionSet.objects.filter(generation_status__in=("QUEUED", "GENERATING"),
        next_attempt_at__lte=timezone.now()).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=timezone.now()))
    for pk in due.order_by("next_attempt_at").values_list("pk", flat=True)[:100]:
        dispatch(pk)
    for pk in QuestionReviewDelivery.objects.filter(delivered_at__isnull=True,
            next_attempt_at__lte=timezone.now()).order_by("next_attempt_at").values_list("pk", flat=True)[:100]:
        deliver_question_review.run(str(pk))


def dispatch_review_delivery(delivery_id):
    try:
        deliver_question_review.apply_async(args=[str(delivery_id)], retry=False)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Question review notification deferred delivery_id=%s", delivery_id)


@shared_task(ignore_result=True)
def deliver_question_review(delivery_id):
    """In-app notification and delivery acknowledgement share a DB commit."""
    try:
        with transaction.atomic():
            delivery = QuestionReviewDelivery.objects.select_for_update().get(pk=delivery_id)
            if delivery.delivered_at or delivery.next_attempt_at > timezone.now():
                return
            row = delivery.question_set
            # An obsolete, unpublished generation should not send a misleading ready notice.
            if row.generation_number != delivery.generation_number or row.generation_status not in ("READY", "FALLBACK"):
                delivery.delivered_at = timezone.now()
                delivery.save(update_fields=["delivered_at"])
                return
            from ..notifications import queue_notification
            queue_notification(row.application.job.recruiter.user_id, 'question_review_ready', resource_type='question_set',
                resource_id=row.pk, revision=f'generation:{row.generation_number}', links={'application_id': row.application_id},
                notification_id=delivery.pk, deliver_inapp=True)
            delivery.delivered_at = timezone.now()
            delivery.save(update_fields=["delivered_at"])
    except Exception:
        QuestionReviewDelivery.objects.filter(pk=delivery_id, delivered_at__isnull=True).update(
            next_attempt_at=timezone.now() + timedelta(seconds=30))
