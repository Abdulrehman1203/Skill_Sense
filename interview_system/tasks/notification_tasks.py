"""Durable in-app notifications with best-effort email; no hiring signals rendered."""
import logging
from uuid import NAMESPACE_URL, uuid4, uuid5

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import InterfaceError, OperationalError

from ..models import Notification, User

logger = logging.getLogger(__name__)

# Closed vocabulary shared by Notification.type, producers, and rendered content.
EVENT_MESSAGES = {
    "application_received": ("Application received", "A new application has been received for one of your jobs. Sign in to review it."),
    "interview_scheduled": ("Interview scheduled", "Your interview has been scheduled. Sign in to view the details."),
    "score_ready": ("Score ready", "A candidate's score is ready. Sign in to review it."),
    "application_status_changed": ("Application updated", "Your application status has changed. Sign in to view the update."),
}


def event_name(event):
    name = event.get("type") if isinstance(event, dict) else event
    if not isinstance(name, str) or name not in EVENT_MESSAGES:
        raise ValueError("Unknown notification event; expected one of: " + ", ".join(EVENT_MESSAGES))
    return name


@shared_task(
    bind=True, name="interview_system.tasks.notifications.send_notification",
    max_retries=3, autoretry_for=(OperationalError, InterfaceError),
    retry_backoff=True, retry_backoff_max=600,
)
def send_notification(self, user_id, event):
    name = event_name(event)
    subject, message = EVENT_MESSAGES[name]
    user = User.objects.get(pk=user_id)
    # Celery retains task ID on retry. Keep a single durable row across attempts
    # without adding delivery columns or changing the Phase 2 schema.
    delivery_id = self.request.id or str(uuid4())
    notification_id = uuid5(NAMESPACE_URL, f"skillsense-notification:{delivery_id}:{user.pk}:{name}")
    notification, _ = Notification.objects.get_or_create(
        pk=notification_id,
        defaults={"user": user, "type": name, "message": message, "is_read": False},
    )
    try:
        sent = send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False)
        if sent != 1:
            raise RuntimeError("Email backend did not accept the message")
    except Exception as exc:
        # SMTP exception messages can contain credentials: log only their class.
        if self.request.retries >= self.max_retries:
            logger.error("Notification email exhausted retries notification_id=%s error_type=%s", notification.pk, type(exc).__name__)
            return {"notification_id": str(notification.pk), "user_id": str(user.pk), "status": "EMAIL_FAILED"}
        logger.warning("Notification email retry notification_id=%s attempt=%s error_type=%s",
                       notification.pk, self.request.retries + 1, type(exc).__name__)
        raise self.retry(countdown=2 ** self.request.retries) from None
    return {"notification_id": str(notification.pk), "user_id": str(user.pk), "status": "SENT"}
