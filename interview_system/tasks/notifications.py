"""
Notification Celery tasks (Phase 10 stub).

Contains send_notification.
Carries real retry policy (3x exponential backoff) and real DB persistence.
"""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)

RETRY_POLICY = {
    "bind": True,
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "retry_backoff_max": 600,
    "max_retries": 3,
}


@shared_task(**RETRY_POLICY)
def send_notification(self, user_id: str, event: dict) -> dict:
    """
    Phase 10: Dispatch user notification.

    Contract:
      Input: user_id (UUID string), event (dict containing type and payload)
      Output: dict with keys {notification_id, user_id, status}
      DB persistence: Creates a Notification row for user_id.
    """
    from ..models import Notification, User

    logger.info("send_notification task executing for user_id=%s, event=%s (attempt %s)", user_id, event, self.request.retries + 1)

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.error("User %s not found for send_notification", user_id)
        raise

    event_type = event.get("type", "GENERAL")
    message = f"Notification event [{event_type}]: {event}"

    notif = Notification.objects.create(
        user=user,
        type=event_type,
        message=message,
        is_read=False,
    )

    result = {
        "notification_id": str(notif.id),
        "user_id": user_id,
        "status": "SENT",
    }

    logger.info("send_notification completed for user_id=%s, Notification pk=%s", user_id, notif.id)

    return result
