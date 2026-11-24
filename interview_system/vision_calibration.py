"""Publisher-scoped calibration using bounded results from the vision worker."""
from dataclasses import asdict
from datetime import timedelta
import uuid
from django.conf import settings
from django.utils import timezone
from ai.vision.gaze import calibrate_center
from ai.vision.types import Gaze

ATTEMPT_SECONDS = 12
MAX_SAMPLES = 16
MIN_USABLE = 5


def enabled():
    return settings.INTERVIEW_ANALYSIS_GAZE_CALIBRATION_ENABLED and settings.INTERVIEW_ANALYSIS_VISION_ENABLED


def _change(session, value):
    from .video_lifecycle import touch_analysis_locked
    session.gaze_setup = value
    session.save(update_fields=['gaze_setup'])
    touch_analysis_locked(session)


def invalidate_locked(session, *, reconnect=False):
    state = session.gaze_setup
    if state.get('state') == 'collecting':
        _change(session, {'state': 'interrupted', **_passive_context(state)})
    elif reconnect and state.get('state') == 'calibrated':
        _change(session, {**state, 'state': 'invalidated'})


def expire_locked(session, now):
    state = session.gaze_setup
    if state.get('state') == 'collecting' and now.timestamp() >= state['deadline']:
        _change(session, {'state': 'timed_out', **_passive_context(state)})


def _passive_context(state):
    return {'passive': True, 'generation': state['generation']} if state.get('passive') else {}


def control(lease, generation, action, setup=None):
    from .video_lifecycle import _publisher, locked_session, require_open
    with locked_session(lease.session_id) as (_, session):
        _publisher(session, lease, generation)
        now = timezone.now()
        require_open(session, now)
        if not enabled():
            return {'type': 'gaze_calibration', 'action': action, 'state': 'unavailable'}
        expire_locked(session, now)
        state = session.gaze_setup
        if action == 'begin':
            if state.get('state') != 'collecting' or state.get('setup') != setup:
                _change(session, {'state': 'collecting', 'setup': setup, 'generation': str(generation),
                                  'attempt': uuid.uuid4().hex, 'deadline': (now + timedelta(seconds=ATTEMPT_SECONDS)).timestamp(),
                                  'offered': 0, 'processed': 0, 'samples': []})
        elif action == 'resume':
            if state.get('state') in ('calibrated', 'invalidated') and state.get('setup') == setup:
                _change(session, {**state, 'state': 'calibrated', 'generation': str(generation)})
            else:
                _change(session, {'state': 'invalidated'})
        elif action == 'skip':
            _change(session, {'state': 'skipped'})
        elif action == 'cancel':
            _change(session, {'state': 'cancelled'})
        state = session.gaze_setup
        return {'type': 'gaze_calibration', 'action': action, 'state': state.get('state', 'uncalibrated'),
                'usable_samples': len(state.get('samples', ()))}


def frame_context(session, generation, is_sample, size, now):
    """Return None for an invalid phase, otherwise the immutable job context."""
    state = session.gaze_setup
    if not enabled():
        # The legacy helper is isolated from opted-in camera setups.
        return None if is_sample else {'baseline': session.vision_calibration if not state else None}
    expire_locked(session, now)
    state = session.gaze_setup
    # An ordinary publisher can calibrate from the same frames used for other
    # observations. Reconnects and resolution changes require a new baseline.
    if not is_sample and (
        not state or (state.get('passive') and (
            state['generation'] != str(generation)
            or ('size' in state and state['size'] != size)
        ))
    ):
        state = {'state': 'collecting', 'passive': True, 'generation': str(generation),
                 'attempt': uuid.uuid4().hex, 'size': size,
                 'deadline': (now + timedelta(seconds=ATTEMPT_SECONDS)).timestamp(),
                 'offered': 0, 'processed': 0, 'samples': []}
        _change(session, state)
    current = state.get('generation') == str(generation)
    if is_sample:
        if not current or state.get('state') != 'collecting' or state['offered'] >= MAX_SAMPLES:
            return None
        if state.setdefault('size', size) != size:
            _change(session, {'state': 'invalidated'})
            return None
        state['offered'] += 1
        return {'attempt': state['attempt']}
    if current and state.get('state') == 'collecting':
        if state.get('passive'):
            if state['offered'] < MAX_SAMPLES:
                state['offered'] += 1
                return {'passive_attempt': state['attempt']}
            return {'baseline': None}
        return None
    if current and state.get('state') == 'calibrated':
        if state['size'] == size:
            return {'baseline': state['baseline'], 'calibration_id': state['attempt']}
        _change(session, {'state': 'invalidated'})
    return {'baseline': None}


def active_attempt(session, job, now):
    state = session.gaze_setup
    return (enabled() and session.admission_sealed_at is None and state.get('state') == 'collecting'
            and state['attempt'] == (job.vision_context.get('attempt') or job.vision_context.get('passive_attempt'))
            and now.timestamp() < state['deadline']
            and state['generation'] == str(job.generation) and session.connection_generation == job.generation
            and session.connection_state == 'connected'
            and session.connection_lease_until is not None and now < session.connection_lease_until
            and session.connection_auth_expires_at is not None and now < session.connection_auth_expires_at)


def collect_locked(session, job, result, now):
    expire_locked(session, now)
    if not active_attempt(session, job, now):
        return
    state = session.gaze_setup
    state['processed'] += 1
    gaze = result['vision_details']['gaze']
    x, y = gaze['horizontal'], gaze['vertical']
    # Stable off-center iris placement alone is insufficient for a neutral
    # reference when nobody is asked to fixate on a known target.
    if (gaze['reason'] == 'uncalibrated' and x is not None and y is not None
            and (not state.get('passive') or (.42 <= x <= .58 and .30 <= y <= .70))):
        state['samples'].append([x, y])
    if len(state['samples']) >= MIN_USABLE:
        try:
            baseline = calibrate_center([Gaze(horizontal=x, vertical=y) for x, y in state['samples']])
        except ValueError:
            pass
        else:
            _change(session, {key: state[key] for key in ('setup', 'generation', 'attempt', 'size', 'passive')
                              if key in state} | {'state': 'calibrated', 'baseline': asdict(baseline)})
            return
    if state['processed'] == MAX_SAMPLES:
        _change(session, {'state': 'failed', **_passive_context(state)})
    else:
        session.save(update_fields=['gaze_setup'])


def set_session_calibration(lease, generation, samples, *, confirmed_camera_target=False):
    from .video_lifecycle import locked_session, _publisher, require_open
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
