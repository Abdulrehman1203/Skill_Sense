"""Internal calibrated-target service. No new browser/API route in B5."""
from dataclasses import asdict
from django.utils import timezone
from ai.vision.gaze import calibrate_center
from .video_lifecycle import locked_session, _publisher, require_open


def set_session_calibration(lease, generation, samples, *, confirmed_camera_target=False):
    if confirmed_camera_target is not True:
        raise ValueError('Explicit camera-target calibration is required.')
    baseline = calibrate_center(samples)
    with locked_session(lease.session_id) as (_, session):
        _publisher(session, lease, generation)
        require_open(session, timezone.now())
        if session.admitted_count:
            raise ValueError('Calibration is immutable after the first admitted frame.')
        session.vision_calibration = asdict(baseline)
        session.save(update_fields=['vision_calibration'])
        return session.vision_calibration
