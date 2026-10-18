"""Pure deterministic episode fold, shared by live and final calculations.

Only consecutive, known positive endpoint observations contribute duration.
No extrapolation to socket disconnect, gaps, unknown intervals or session end.
"""
from dataclasses import dataclass, asdict
from collections import deque
import hashlib
import json
import math


@dataclass(frozen=True)
class RuleConfig:
    max_gap: float = 1.0
    onset_seconds: float = 2.0
    release_seconds: float = 1.0
    enter_confidence: float = 0.65
    retain_confidence: float = 0.45

    def __post_init__(self):
        if (not all(math.isfinite(v) for v in asdict(self).values()) or not 0 < self.max_gap <= 2
                or not 0 < self.onset_seconds <= 10 or not 0 <= self.release_seconds <= 3
                or not 0 <= self.retain_confidence < self.enter_confidence <= 1):
            raise ValueError('Invalid temporal rule configuration.')

    @property
    def version(self):
        return 'episodes-v1-'+hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


def signal(observation, name, active, config):
    details = observation.get('vision_details')
    if not details or not details['quality']['usable']:
        return None
    threshold = config.retain_confidence if active else config.enter_confidence
    detections = observation['detected_objects']
    if name == 'phone_visible':
        return any(d['label'] == 'phone' and d['confidence'] >= threshold for d in detections)
    if name == 'multiple_people_observed':
        return details['face_count'] >= 2 or sum(d['label'] == 'person' and d['confidence'] >= threshold for d in detections) >= 2
    if name == 'no_face_observed':
        return details['face_count'] == 0
    direction = details['gaze']['direction']
    return None if direction == 'unknown' else direction == 'away'


def evaluate(samples, config=None):
    """samples: iterable of (server_seconds, admission_number, observation).

One caller/session per fold. State is local and bounded to four episodes. Input
must be chronological, identities unique; replay the same persisted ordered
ledger for a reproducible final result. Caller stores a bounded projection.
"""
    config = config or RuleConfig()
    names = ('phone_visible', 'multiple_people_observed', 'no_face_observed', 'gaze_away')
    states = {name: None for name in names}
    events = deque(maxlen=100)
    episode_counts = {name: 0 for name in names}
    totals = {name: 0. for name in names}
    previous = None

    def end(name):
        state = states[name]
        if state and state['duration'] >= config.onset_seconds:
            episode_counts[name] += 1
            events.append({'type': name, 'episode_key': name+':'+str(state['first_ordinal']),
                           'start': state['start'], 'end': state['last_positive'],
                           'observed_seconds': round(state['duration'], 6), 'rules_version': config.version})
        states[name] = None

    for timestamp, ordinal, observation in samples:
        if (type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                or type(ordinal) is not int or ordinal <= 0):
            raise ValueError('Invalid observation identity/time.')
        if previous and (timestamp < previous[0] or ordinal <= previous[1]):
            raise ValueError('Observations must be ordered and unique.')
        gap = timestamp-previous[0] if previous else 0.
        for name in names:
            state = states[name]
            value = signal(observation, name, state is not None, config)
            if value is None or (previous and gap > config.max_gap):
                end(name)
                state = None
            if value is None:
                continue
            if value:
                if state is None:
                    states[name] = {'start': timestamp, 'last_positive': timestamp,
                                    'first_ordinal': ordinal, 'duration': 0., 'previous_positive': True, 'negative_since': None}
                else:
                    if state['previous_positive']:
                        state['duration'] += gap
                        totals[name] += gap
                    state.update(last_positive=timestamp, previous_positive=True, negative_since=None)
            elif state:
                state['previous_positive'] = False
                state['negative_since'] = timestamp if state['negative_since'] is None else state['negative_since']
                if timestamp-state['negative_since'] >= config.release_seconds:
                    end(name)
        previous = timestamp, ordinal
    for name in names:
        end(name)
    return {'rules_version': config.version, 'events': list(events),
            'episode_counts': episode_counts, 'events_truncated': sum(episode_counts.values()) > len(events),
            'observed_seconds': {name: round(value, 6) for name, value in totals.items()}}
