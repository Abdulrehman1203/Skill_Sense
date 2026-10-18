"""Persistence validation without Django or model-runtime imports."""
import json
import math
import re
from .types import Calibration

GAZE_REASONS = {'uncalibrated', 'no_face', 'multiple_faces', 'geometry_unavailable',
                'head_pose', 'eyes_unusable', 'eyes_disagree', 'quality_unusable', 'approximate'}
QUALITY_REASONS = {'usable', 'too_dark', 'too_bright', 'low_contrast', 'blurred'}


def validate_details(details, result):
    if not isinstance(details, dict) or set(details) != {'schema_version', 'quality', 'face_count', 'face_limit',
            'gaze', 'expression', 'calibration', 'config_version'}:
        raise ValueError('Invalid vision detail schema.')
    if type(details['schema_version']) is not int or details['schema_version'] != 1:
        raise ValueError('Unsupported vision detail version.')
    if len(json.dumps(details, allow_nan=False).encode()) > 4096:
        raise ValueError('Vision details exceed size policy.')
    q = details['quality']
    if (not isinstance(q, dict) or set(q) != {'usable', 'reason', 'brightness', 'contrast', 'sharpness'}
            or type(q['usable']) is not bool or q['reason'] not in QUALITY_REASONS
            or q['usable'] != (q['reason'] == 'usable')):
        raise ValueError('Invalid quality result.')
    for key, maximum in (('brightness', 255), ('contrast', 255), ('sharpness', 10000000)):
        if type(q[key]) not in (int, float) or not math.isfinite(q[key]) or not 0 <= q[key] <= maximum:
            raise ValueError('Invalid quality metric.')
    count = details['face_count']
    if type(details['face_limit']) is not int or not 2 <= details['face_limit'] <= 5:
        raise ValueError('Invalid face detection cap.')
    if ((q['usable'] and (type(count) is not int or not 0 <= count <= details['face_limit']))
            or (not q['usable'] and count is not None) or result['face_count'] != count):
        raise ValueError('Invalid face availability.')
    gaze = details['gaze']
    if (not isinstance(gaze, dict) or set(gaze) != {'direction', 'reason', 'horizontal', 'vertical'}
            or gaze['direction'] not in ('unknown', 'centered', 'away')
            or gaze['reason'] not in GAZE_REASONS or gaze['direction'] != result['gaze_direction']
            or (gaze['direction'] != 'unknown' and (gaze['reason'] != 'approximate' or count != 1 or not q['usable']))):
        raise ValueError('Invalid gaze availability.')
    for axis in ('horizontal', 'vertical'):
        value = gaze[axis]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1):
            raise ValueError('Invalid gaze geometry.')
    baseline = Calibration.from_dict(details['calibration'])
    if gaze['direction'] != 'unknown' and (baseline is None or gaze['horizontal'] is None or gaze['vertical'] is None):
        raise ValueError('Approximate gaze requires explicit calibration.')
    if details['expression'] != {'status': 'disabled', 'label': None}:
        raise ValueError('Expression inference is disabled.')
    if not isinstance(details['config_version'], str) or not re.fullmatch(r'config-v1-[0-9a-f]{16}', details['config_version']):
        raise ValueError('Missing configuration provenance.')
    if not q['usable'] and (gaze['direction'] != 'unknown' or result['detected_objects']):
        raise ValueError('Unusable input cannot imply observed absence.')
