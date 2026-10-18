"""B6 real ledger aggregation; existing task name retained, no scoring dispatch."""
from celery import shared_task
from django.db import OperationalError, InterfaceError
from django.db import transaction


@shared_task(bind=True, autoretry_for=(OperationalError, InterfaceError),
             retry_backoff=True, retry_backoff_max=30, max_retries=3)
def aggregate_behavioral(self, session_id: str) -> dict:
    from ..analysis_reporting import aggregate_session, deliver_notification
    result = aggregate_session(session_id)
    transaction.on_commit(lambda: deliver_notification(session_id))
    return result


from .scoring_tasks import generate_candidate_score  # Existing public import path.
