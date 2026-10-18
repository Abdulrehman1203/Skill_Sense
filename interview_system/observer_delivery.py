"""B6 snapshot seam. Notifications are references; never forward arbitrary events."""
from datetime import datetime
import math
import uuid

COUNTS = {'received', 'admitted', 'processed', 'dropped', 'superseded', 'failed', 'expired', 'valid_gaze', 'unknown_gaze'}
STATES = ('pending', 'processing', 'finalizing', 'complete', 'partial', 'failed')
KINDS = ('face', 'gaze', 'person', 'phone', 'expression')
VALUES = ('unknown', 'none', 'one', 'multiple', 'centered', 'away', 'present', 'absent', 'unavailable')


async def get_snapshot(lease):
    from channels.db import database_sync_to_async
    from .analysis_reporting import report_response
    from .socket_db import _call
    from .socket_auth import check_lease, SocketAuthorizationError
    def fetch():
        check_lease(lease)
        if lease.purpose != 'observer':
            raise SocketAuthorizationError('access_denied')
        return report_response(lease.session_id, lease.user_id, snapshot=True)['snapshot']
    return await database_sync_to_async(_call)(fetch)


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('Invalid timestamp.')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError('Invalid timestamp.')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def validate_snapshot(snapshot, session_id):
    if (not isinstance(snapshot, dict) or set(snapshot) != {'type', 'session_id', 'revision', 'server_ts', 'status', 'coverage', 'observations'}
            or snapshot['type'] != 'analysis.update' or snapshot['session_id'] != str(session_id)
            or type(snapshot['revision']) is not int or snapshot['revision'] < 0 or snapshot['status'] not in STATES):
        raise ValueError('Invalid snapshot envelope.')
    _timestamp(snapshot['server_ts'])
    coverage = snapshot['coverage']
    if not isinstance(coverage, dict) or set(coverage) != COUNTS or any(type(v) is not int or v < 0 for v in coverage.values()):
        raise ValueError('Invalid coverage.')
    observations = snapshot['observations']
    if not isinstance(observations, list) or len(observations) > 100:
        raise ValueError('Invalid observation window.')
    for item in observations:
        if not isinstance(item, dict) or set(item) != {'id', 'kind', 'value', 'confidence', 'observed_at', 'boxes'}:
            raise ValueError('Invalid observation.')
        uuid.UUID(item['id'])
        if (item['kind'] not in KINDS or item['value'] not in VALUES
                or (item['confidence'] is not None and not _number(item['confidence']))
                or (item['kind'] == 'expression' and item['value'] != 'unavailable')):
            raise ValueError('Invalid observation values.')
        _timestamp(item['observed_at'])
        boxes = item['boxes']
        if not isinstance(boxes, list) or len(boxes) > 20:
            raise ValueError('Invalid boxes.')
        for box in boxes:
            if (not isinstance(box, list) or len(box) != 4 or not all(_number(v) for v in box)
                    or box[0]+box[2] > 1 or box[1]+box[3] > 1):
                raise ValueError('Invalid box.')
    return snapshot
