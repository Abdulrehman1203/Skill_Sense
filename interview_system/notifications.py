"""Publish event tasks only after commit, always to the broker (even in eager mode)."""
import logging

from django.db import transaction

from .tasks.notification_tasks import event_name

logger = logging.getLogger(__name__)


def queue_notification(user_id, event):
    from .tasks.notifications import send_notification

    name, recipient = event_name(event), str(user_id)

    def publish():
        try:
            # send_task bypasses task_always_eager: only workers may send email.
            send_notification.app.send_task(
                send_notification.name, args=[recipient], kwargs={"event": {"type": name}},
                queue=send_notification.app.conf.task_default_queue,
            )
        except Exception:
            logger.error("Notification enqueue failed event=%s; broker delivery required", name)

    transaction.on_commit(publish)
