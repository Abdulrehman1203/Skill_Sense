"""Bounded candidate feedback from already processed frames, with no stored alerts."""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from ai.vision.schema import QUALITY_REASONS
from .video_lifecycle import ACTIVE_JOBS, _publisher, locked_session, require_open

MAX_ROWS = 24
MAX_GAP = 1.0
MAX_AGE = 2.0


def enabled():
    return settings.INTERVIEW_ANALYSIS_VISION_ENABLED and (
        settings.INTERVIEW_ANALYSIS_CAMERA_QUALITY_PROMPTS_ENABLED
        or settings.INTERVIEW_ANALYSIS_NO_FACE_PROMPTS_ENABLED
    )


def _sample(state, pipeline, usable, reason, faces):
    if state != 'processed' or pipeline != 'vision-v1':
        return 'unknown'
    if usable is False and isinstance(reason, str) and reason != 'usable' and reason in QUALITY_REASONS:
        return 'poor_quality'
    if usable is True and reason == 'usable' and type(faces) is int and faces >= 0:
        return 'no_face' if faces == 0 else 'face_visible'
    return 'unknown'


def assessment(rows, now, *, last_sequence, quality_enabled, no_face_enabled):
    """Rows are newest first. Pending tail jobs can lag, but gaps break support."""
    if not rows or rows[0][0] != last_sequence:
        return 'unknown'
    # A frame has not had a chance to settle while it is in flight. Ignore only
    # the trailing in-flight prefix. Older unfinished work breaks continuity.
    offset = next((index for index, row in enumerate(rows) if row[2] not in ACTIVE_JOBS), len(rows))
    if offset == len(rows):
        return 'unknown'
    newest_sequence, newest_at, *evidence = rows[offset]
    if newest_sequence != last_sequence - offset or not 0 <= (now - newest_at).total_seconds() <= MAX_AGE:
        return 'unknown'
    if any(newer[0] != older[0] + 1 or not 0 < (newer[1] - older[1]).total_seconds() <= MAX_GAP
           for newer, older in zip(rows[:offset], rows[1:offset + 1])):
        return 'unknown'
    current = _sample(*evidence)
    if current == 'unknown':
        return current

    previous_sequence, previous_at = newest_sequence, newest_at
    issue_count, usable_count = 1, 1
    issue_seconds, usable_seconds = 0., 0.
    same_issue = True
    for sequence, received_at, *evidence in rows[offset + 1:]:
        gap = (previous_at - received_at).total_seconds()
        if sequence != previous_sequence - 1 or not 0 < gap <= MAX_GAP:
            break
        kind = _sample(*evidence)
        if kind == 'unknown':
            break
        same_issue = same_issue and kind == current
        if same_issue:
            issue_count += 1
            issue_seconds = (newest_at - received_at).total_seconds()
        if current == 'poor_quality' or kind == 'poor_quality':
            if not same_issue:
                break
        else:
            usable_count += 1
            usable_seconds = (newest_at - received_at).total_seconds()
        previous_sequence, previous_at = sequence, received_at

    if issue_count >= 4 and issue_seconds >= 3.0:
        if (current == 'poor_quality' and quality_enabled) or (current == 'no_face' and no_face_enabled):
            return current
    if current == 'face_visible' and issue_count >= 3 and issue_seconds >= 1.0:
        return 'face_visible'
    if current != 'poor_quality' and usable_count >= 3 and usable_seconds >= 1.0:
        return 'usable'
    return 'unknown'


def status(lease, generation):
    with locked_session(lease.session_id) as (_, session):
        _publisher(session, lease, generation)
        now = timezone.now()
        require_open(session, now)
        if not enabled() or session.gaze_setup.get('state') == 'collecting':
            return 'unknown'
        rows = list(session.frame_jobs.filter(generation=generation, received_at__gte=now-timedelta(seconds=8))
                    .order_by('-sequence').values_list(
                        'sequence', 'received_at', 'state', 'observation__pipeline_version',
                        'observation__vision_details__quality__usable',
                        'observation__vision_details__quality__reason', 'observation__face_count')[:MAX_ROWS])
        return assessment(rows, now, last_sequence=session.last_sequence,
                          quality_enabled=settings.INTERVIEW_ANALYSIS_CAMERA_QUALITY_PROMPTS_ENABLED,
                          no_face_enabled=settings.INTERVIEW_ANALYSIS_NO_FACE_PROMPTS_ENABLED)
