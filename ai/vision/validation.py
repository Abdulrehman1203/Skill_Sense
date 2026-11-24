"""Compare consented, clip-relative labels with sampled model and rule output."""
from collections import Counter
import math

from .rules import CAMERA_SIGNALS, signal_at_threshold


def _intervals(rows):
    if not isinstance(rows, list) or len(rows) > 1800:
        raise ValueError('Invalid staged interval count.')
    previous_end = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid staged interval.')
        start, end = row.get('start'), row.get('end')
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or not previous_end <= start < end):
            raise ValueError('Staged intervals must be ordered and disjoint.')
        previous_end = end
        yield row


def staged_labels(clip):
    labels = {name: [] for name in CAMERA_SIGNALS}
    for row in _intervals(clip.get('labels', [])):
        phone, face, people = (row.get(key) for key in ('phone_visible', 'face_count', 'person_count'))
        if phone is not None and type(phone) is not bool:
            raise ValueError('Invalid phone label.')
        for value in (face, people):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('Invalid staged count label.')
        for name, value in zip(CAMERA_SIGNALS, (phone, None if people is None else people >= 2,
                                                 None if face is None else face == 0)):
            if value is not None:
                labels[name].append((row['start'], row['end'], value))
    expected = clip.get('expected_episodes', {})
    if not isinstance(expected, dict) or set(expected) - set(CAMERA_SIGNALS):
        raise ValueError('Invalid staged episode labels.')
    return labels, {name: [(row['start'], row['end']) for row in _intervals(rows)]
                    for name, rows in expected.items()}


def _overlap(start, end, window):
    return max(0, min(end, window[1]) - max(start, window[0]))


def compare(timeline, folded, annotations, config):
    labels, expected = annotations
    result = {}
    for name in CAMERA_SIGNALS:
        counters = {threshold: Counter(dict(missed_frames=0, false_frames=0, true_positive_frames=0,
                                             true_negative_frames=0, unknown_frames=0, unlabeled_frames=0))
                    for threshold in ('model', 'entry')}
        frame_values = []
        for seconds, _, observation in timeline:
            truth = next((value for start, end, value in labels[name] if start <= seconds < end), None)
            raw = signal_at_threshold(observation, name, 0.)
            qualified = signal_at_threshold(observation, name, config.enter_confidence)
            frame_values.append((seconds, raw, qualified))
            for threshold, value in (('model', raw), ('entry', qualified)):
                if truth is None:
                    key = 'unlabeled_frames'
                elif value is None:
                    key = 'unknown_frames'
                elif truth == value:
                    key = 'true_positive_frames' if value else 'true_negative_frames'
                else:
                    key = 'missed_frames' if truth else 'false_frames'
                counters[threshold][key] += 1
        result[name] = {'frames': counters, 'episodes': None}
        if name not in expected:
            continue
        events = [row for row in folded['events'] if row['type'] == name]
        if len(events) != folded['episode_counts'][name]:
            continue  # The bounded event window cannot support episode matching.
        remaining = set(range(len(events)))
        misses = []
        for start, end in expected[name]:
            # Both lists are ordered and disjoint within a signal. Matching
            # the earliest eligible event preserves later matching options.
            match = next((index for index in sorted(remaining) if _overlap(
                start, end, (events[index]['start'], events[index]['end'])) > 0), None)
            if match is not None:
                remaining.remove(match)
                continue
            window = [(raw, qualified) for time, raw, qualified in frame_values if start <= time < end]
            reason = ('no_usable_samples' if not any(raw is not None for raw, _ in window)
                      else 'no_model_detection' if not any(raw for raw, _ in window)
                      else 'below_episode_entry_confidence' if not any(qualified for _, qualified in window)
                      else 'episode_duration_or_continuity')
            misses.append({'start': start, 'end': end, 'reason': reason})
        unmatched, unknown = [], []
        for index in sorted(remaining):
            event = events[index]
            start, end = event['start'], event['end']
            covered = sum(_overlap(start, end, row) for row in labels[name])
            target = unmatched if covered >= end - start - 1e-6 else unknown
            target.append({'start': start, 'end': end, 'reason': 'no_matching_labeled_episode'
                           if target is unmatched else 'incomplete_episode_labels'})
        result[name]['episodes'] = {'matched': len(expected[name]) - len(misses),
                                    'missed': misses, 'false': unmatched, 'unknown': unknown}
    return result
