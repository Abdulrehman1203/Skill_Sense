"""B6 CPU-only fold of the admitted ledger. No model/runtime initialization."""
from collections import deque
from datetime import timezone as dt_timezone
import uuid
from django.utils import timezone

from ai.vision.rules import RuleConfig, evaluate
from .models import FrameJob
from .video_lifecycle import _validate_observation


def iso(value):
    return value.astimezone(dt_timezone.utc).isoformat().replace('+00:00', 'Z')


def observation_projection(frame, result):
    details = result['vision_details']
    usable = details['quality']['usable']
    count = details['face_count']
    values = []
    for kind in ('face', 'gaze', 'person', 'phone', 'expression'):
        boxes, confidence = [], None
        if kind == 'face':
            value = 'unknown' if count is None else 'none' if count == 0 else 'one' if count == 1 else 'multiple'
        elif kind == 'gaze':
            value = result['gaze_direction']
        elif kind == 'expression':
            value = 'unavailable'
        else:
            detections = [d for d in result['detected_objects'] if d['label'] == kind]
            value = 'unknown' if not usable else 'present' if detections else 'none'
            boxes = [d['box'] for d in detections]
            confidence = max((d['confidence'] for d in detections), default=None)
        values.append({'id': str(uuid.uuid5(frame.pk, kind)), 'kind': kind, 'value': value,
            'confidence': confidence, 'observed_at': iso(frame.ts), 'boxes': boxes})
    return values


def calculate(session, base_coverage):
    config = RuleConfig()
    coverage = {**base_coverage, 'valid_gaze': 0, 'unknown_gaze': 0,
                'usable_frames': 0, 'unusable_frames': 0, 'unsupported_frames': 0}
    versions = {name: set() for name in ('pipeline_version', 'model_version', 'rules_version', 'config_version')}
    recent = deque(maxlen=20)
    known = {'gaze': 0., 'objects': 0., 'face': 0.}
    centered = 0.
    previous = None
    rows_seen = 0
    versions_truncated = False

    def remember(name, value):
        nonlocal versions_truncated
        if not isinstance(value, str) or not value or len(value) > 100:
            return
        if len(versions[name]) < 16:
            versions[name].add(value)
        elif value not in versions[name]:
            versions_truncated = True

    def samples():
        nonlocal previous, centered, rows_seen, versions_truncated
        jobs = FrameJob.objects.filter(session=session).select_related('observation').order_by('admission_number')
        for job in jobs.iterator(chunk_size=200):
            rows_seen += 1
            if rows_seen % 200 == 0 and timezone.now() >= session.finalization_lease_until:
                raise TimeoutError('Report claim expired.')
            timestamp = job.received_at.timestamp()
            result, frame = {'vision_details': None}, getattr(job, 'observation', None)
            valid = False
            if job.state == 'processed':
                if frame is not None:
                    result = {'gaze_direction': frame.gaze_direction, 'face_count': frame.face_count,
                              'detected_objects': frame.detected_objects, 'vision_details': frame.vision_details}
                    provenance = {name: getattr(frame, name) for name in ('pipeline_version', 'model_version', 'rules_version')}
                    for name, value in provenance.items():
                        remember(name, value)
                    if isinstance(frame.vision_details, dict):
                        remember('config_version', frame.vision_details.get('config_version'))
                    try:
                        if provenance['pipeline_version'] != 'vision-v1' or provenance['rules_version'] != config.version:
                            raise ValueError('Unsupported evidence provenance.')
                        _validate_observation(result, provenance)
                        valid = True
                    except (ValueError, TypeError, KeyError):
                        pass
                if not valid:
                    coverage['unsupported_frames'] += 1
                    result = {'vision_details': None}
                else:
                    recent.append(observation_projection(frame, result))
                    key = 'usable_frames' if frame.vision_details['quality']['usable'] else 'unusable_frames'
                    coverage[key] += 1
                if valid and result['gaze_direction'] in ('centered', 'away'):
                    coverage['valid_gaze'] += 1
                else:
                    coverage['unknown_gaze'] += 1
            details = result.get('vision_details')
            usable = bool(details and details['quality']['usable'])
            gaze = result.get('gaze_direction', 'unknown') if usable else 'unknown'
            if previous is not None:
                gap = timestamp-previous[0]
                if 0 <= gap <= config.max_gap and usable and previous[1]:
                    known['objects'] += gap
                    known['face'] += gap
                    # A transition interval is not allocated to either class.
                    if gaze != 'unknown' and gaze == previous[2]:
                        known['gaze'] += gap
                        if gaze == 'centered':
                            centered += gap
            previous = timestamp, usable, gaze
            yield timestamp, job.admission_number, result

    episodes = evaluate(samples(), config)
    mixed = versions_truncated or any(len(value) > 1 for value in versions.values())
    counts, durations = {}, {}
    for kind in ('phone_visible', 'multiple_people_observed', 'no_face_observed', 'gaze_away'):
        available = known['gaze' if kind == 'gaze_away' else 'face' if kind == 'no_face_observed' else 'objects'] > 0
        counts[kind] = episodes['episode_counts'].get(kind, 0) if available else None
        durations[kind] = episodes['observed_seconds'].get(kind, 0) if available else None
    final = session.admission_sealed_at is not None and session.analysis_ready_at is not None
    if not final:
        status = 'finalizing' if session.admission_sealed_at else 'processing' if session.admitted_count else 'pending'
    elif coverage['usable_frames'] == 0:
        status = 'failed'
    elif (mixed or coverage['dropped'] or coverage['superseded'] or coverage['failed'] or coverage['expired']
          or coverage['unusable_frames'] or coverage['unsupported_frames'] or coverage['unknown_gaze'] or known['gaze'] == 0):
        status = 'partial'
    else:
        status = 'complete'
    return {'schema_version': 1, 'status': status, 'final': final,
        'reason': 'no_usable_evidence' if final and status == 'failed' else 'mixed_provenance' if mixed else None,
        'coverage': {**coverage, 'known_seconds': {key: round(value, 6) for key, value in known.items()}},
        'gaze_centered_pct': round(100*centered/known['gaze'], 4) if known['gaze'] > 0 else None,
        'episodes': {**episodes, 'episode_counts': counts, 'observed_seconds': durations},
        'versions': {name: sorted(value) for name, value in versions.items()},
        'provenance_limit': 16, 'versions_truncated': versions_truncated,
        'validation_status': 'real_world_accuracy_outstanding',
        'expression': {'status': 'disabled', 'label': None}, 'scoring_eligible': False,
        'observations': [item for group in recent for item in group],
        'ledger_rows_seen': rows_seen}
