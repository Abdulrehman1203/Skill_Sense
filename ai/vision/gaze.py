"""Approximate iris position, not eye-contact/attention measurement.

IMAGE mode has no tracker. Calibration is caller-owned, per session and immutable
for an admitted job. Two faces, uncertain pose/eye geometry or no baseline -> UNKNOWN.
"""
import math
from statistics import median
from .types import Calibration, Gaze


def calibrate_center(samples):
    """Caller must attest all samples were captured looking at the camera target."""
    if not isinstance(samples, (list, tuple)) or not 5 <= len(samples) <= 30:
        raise ValueError('Calibration sample count must be between five and thirty.')
    good = [(s.horizontal, s.vertical) for s in samples
            if s.horizontal is not None and s.vertical is not None]
    if len(good) < 5:
        raise ValueError('At least five usable explicit calibration samples are required.')
    for x, y in good:
        Calibration(x, y)
    if max(x for x, _ in good)-min(x for x, _ in good) > .15 or max(y for _, y in good)-min(y for _, y in good) > .2:
        raise ValueError('Calibration target observations are not stable.')
    return Calibration(median(x for x, _ in good), median(y for _, y in good))


def _eye_image_usable(rgb, x0, y0, x1, y1):
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        return False
    height, width = rgb.shape[:2]
    left, right = max(0, int(x0 * width)), min(width, math.ceil(x1 * width))
    top, bottom = max(0, int(y0 * height)), min(height, math.ceil(y1 * height))
    if right - left < 4 or bottom - top < 3:
        return False
    gray = rgb[top:bottom, left:right].mean(axis=2)
    # Reject clipped or textureless eye regions even when the full image is usable.
    return (35 <= gray.mean() <= 225 and gray.std() >= 8
            and (gray >= 245).mean() <= .25)


def estimate(landmarks, matrix, calibration=None, *, rgb=None):
    if len(landmarks) != 1:
        return Gaze(reason='no_face' if not landmarks else 'multiple_faces')
    points = landmarks[0]
    if len(points) < 478 or matrix is None:
        return Gaze(reason='geometry_unavailable')
    # Rotation matrix from FaceLandmarker. Broad safety gate, not a pose estimate
    # calibrated to this camera; reject >~25deg yaw / ~20deg pitch.
    try:
        yaw = math.degrees(math.atan2(float(matrix[0][2]), float(matrix[2][2])))
        pitch = math.degrees(math.atan2(-float(matrix[1][2]),
                    math.hypot(float(matrix[0][2]), float(matrix[2][2]))))
        if not math.isfinite(yaw+pitch) or abs(yaw) > 25 or abs(pitch) > 20:
            return Gaze(reason='head_pose')
        ratios = []
        for left, right, top, bottom, iris in ((33, 133, 159, 145, 468), (362, 263, 386, 374, 473)):
            x0, x1 = sorted((points[left].x, points[right].x))
            y0, y1 = sorted((points[top].y, points[bottom].y))
            if x1-x0 < 0.015 or y1-y0 < 0.004 or (y1-y0)/(x1-x0) < 0.12:
                return Gaze(reason='eyes_unusable')
            if rgb is not None and not _eye_image_usable(rgb, x0, y0, x1, y1):
                return Gaze(reason='eyes_unusable')
            ratios.append(((points[iris].x-x0)/(x1-x0), (points[iris].y-y0)/(y1-y0)))
        if any(not math.isfinite(v) or not 0 <= v <= 1 for pair in ratios for v in pair):
            return Gaze(reason='eyes_unusable')
        if abs(ratios[0][0]-ratios[1][0]) > 0.2 or abs(ratios[0][1]-ratios[1][1]) > 0.25:
            return Gaze(reason='eyes_disagree')
        horizontal = sum(p[0] for p in ratios)/2
        vertical = sum(p[1] for p in ratios)/2
    except (IndexError, TypeError, AttributeError, ValueError, ZeroDivisionError):
        return Gaze(reason='geometry_unavailable')
    if calibration is None:
        return Gaze(reason='uncalibrated', horizontal=horizontal, vertical=vertical)
    direction = ('centered' if abs(horizontal-calibration.horizontal) <= 0.18
                 and abs(vertical-calibration.vertical) <= 0.25 else 'away')
    return Gaze(direction, 'approximate', horizontal, vertical)
