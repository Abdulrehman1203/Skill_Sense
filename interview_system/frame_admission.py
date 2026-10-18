"""B4 bounded private payload ownership + durable PostgreSQL admission intent."""
from dataclasses import dataclass
from channels.db import database_sync_to_async
from django.conf import settings
from django.db import transaction
from redis.exceptions import RedisError
from .socket_db import db_budget
from .video_lifecycle import admit_frame
from .vision_payloads import PayloadCapacity, PayloadStore


@dataclass(frozen=True)
class AdmissionDecision:
    status: str
    reason: str | None = None


def submit_sync(*, lease, generation, sequence, capture_ts, received_at, jpeg):
    if not settings.INTERVIEW_ANALYSIS_VISION_ENABLED:
        return AdmissionDecision('dropped', 'capacity')
    store = PayloadStore()
    def cleanup(job_id):
        try:
            store.delete(job_id)
        except RedisError:
            pass  # Bounded TTL orphan; never log credentials or image data.
    def retain(job, replaced):
        store.put(job, jpeg.data)
        if replaced:
            transaction.on_commit(lambda: cleanup(replaced.pk))
    try:
        from .vision_inference import worker_ready
        if not worker_ready():
            return AdmissionDecision('dropped', 'capacity')
        with db_budget():
            job, reason = admit_frame(lease, generation, sequence, capture_ts,
                                     retain_payload=retain, received_at=received_at)
        return AdmissionDecision('accepted' if job else 'dropped', reason)
    except (PayloadCapacity, RedisError):
        return AdmissionDecision('dropped', 'capacity')
    finally:
        store.close()


async def submit_frame(**kwargs):
    # ASGI does no broker I/O. Recovery publishes only committed ledger rows.
    return await database_sync_to_async(submit_sync)(**kwargs)
