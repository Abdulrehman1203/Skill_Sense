"""Sync DB operations run off-loop with PostgreSQL statement/lock time budgets."""
from contextlib import contextmanager

from channels.db import database_sync_to_async
from django.conf import settings
from django.db import connection, DatabaseError

from .socket_auth import check_lease, redeem_ticket
from .video_lifecycle import (acquire_publisher, disconnect_publisher, heartbeat_publisher,
                              locked_session, require_open, record_frame_drop, _publisher)
from django.utils import timezone


@contextmanager
def db_budget():
    previous = None
    if connection.vendor == 'postgresql':
        milliseconds = str(max(100, int(settings.INTERVIEW_ANALYSIS_IO_TIMEOUT_SECONDS * 500)))
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")
            previous = cursor.fetchone()
            cursor.execute("SELECT set_config('statement_timeout', %s, false), set_config('lock_timeout', %s, false)",
                           [milliseconds, milliseconds])
    try:
        yield
    finally:
        if previous is not None:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('statement_timeout', %s, false), set_config('lock_timeout', %s, false)", previous)
            except DatabaseError:
                connection.close()


def _call(function, *args):
    with db_budget():
        return function(*args)


def _check_publisher(lease, generation):
    with locked_session(lease.session_id) as (_, session):
        _publisher(session, lease, generation)
        require_open(session, timezone.now())


async def redeem(secret, session_id, purpose):
    return await database_sync_to_async(_call)(redeem_ticket, secret, session_id, purpose)


async def acquire(lease):
    return await database_sync_to_async(_call)(acquire_publisher, lease)


async def check(lease, generation=None):
    if generation is None:
        return await database_sync_to_async(_call)(check_lease, lease)
    return await database_sync_to_async(_call)(_check_publisher, lease, generation)


async def heartbeat(lease, generation):
    return await database_sync_to_async(_call)(heartbeat_publisher, lease, generation)


async def disconnect(session_id, generation):
    return await database_sync_to_async(_call)(disconnect_publisher, session_id, generation)


async def drop(lease, generation, sequence):
    return await database_sync_to_async(_call)(record_frame_drop, lease, generation, sequence)
