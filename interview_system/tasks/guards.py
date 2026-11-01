"""Acknowledge deliveries left behind after their durable resource was deleted."""
from functools import wraps
import logging

logger = logging.getLogger(__name__)


def skip_missing_resource(*models):
    """Catch only named model lookups, never database or provider failures.

    Apply inside shared_task so transaction contexts unwind before acknowledging
    an orphan. Redis deliveries can outlive deleted records or restored databases.
    """
    missing = tuple(model.DoesNotExist for model in models)

    def decorate(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except missing:
                logger.info('Skipping orphaned task task=%s reason=RESOURCE_MISSING', function.__name__)
                return {'status': 'SKIPPED', 'reason': 'RESOURCE_MISSING'}
        return guarded
    return decorate
