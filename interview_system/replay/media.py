import hashlib
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests
from django.conf import settings

from . import storage

MAX_DRIFT = 0.35
MIN_COVERAGE = 2.0
WIDTH, HEIGHT, FPS = 640, 360, 24


class MediaUnavailable(Exception):
    pass


class Unaligned(Exception):
    pass


class InvalidMedia(Exception):
    pass


@dataclass
class Clip:
    source: Path
    offset: float
    duration: float
    scale: float
    incomplete: bool
    drift: float
    uncertainty: float


def run(program, args, timeout):
    command = [program, *args]
    if sys.platform == 'linux':
        # Keep ffmpeg bounded even if its Celery child dies mid-encode.
        command = ['timeout', '--kill-after=5s', f'{timeout}s', *command]
    try:
        return subprocess.run(
            command, capture_output=True, check=True, timeout=timeout + 6,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        raise InvalidMedia from None


def input_options(path):
    formats = {'.webm': 'matroska', '.mp4': 'mov', '.wav': 'wav'}
    return ['-protocol_whitelist', 'file', '-f', formats[path.suffix]]


def probe(path, selector):
    result = run('ffprobe', [
        '-v', 'error', *input_options(path), '-select_streams', selector,
        '-show_entries', 'format=duration:stream=codec_name,start_time,duration', '-of', 'json', str(path),
    ], 20)
    metadata = json.loads(result)
    if not metadata.get('streams'):
        raise InvalidMedia
    stream = metadata['streams'][0]
    try:
        start = float(stream.get('start_time', 0))
        duration = float(stream.get('duration') or metadata['format']['duration'])
    except (ValueError, KeyError):
        # An interrupted WebM often has an unknown container duration. Decode
        # timestamps from its surviving packets without rewriting the input.
        result = run('ffprobe', [
            '-v', 'error', *input_options(path), '-select_streams', selector,
            '-show_entries', 'packet=pts_time,duration_time', '-of', 'csv=p=0', str(path),
        ], 30)
        packets = []
        for row in result.decode().splitlines():
            pts, _, length = row.partition(',')
            try:
                packets.append((float(pts), float(length) if length not in ('', 'N/A') else 0))
            except ValueError:
                continue
        if not packets:
            raise InvalidMedia
        start = min(pts for pts, _ in packets)
        duration = max(pts + length for pts, length in packets) - start
    if not math.isfinite(duration) or not math.isfinite(start) or duration <= 0:
        raise InvalidMedia
    return stream['codec_name'], start, duration


def download_audio(call, path):
    if call.get('call_status') != 'ended':
        raise MediaUnavailable
    # Retell documents recording_url as the mixed recording and multi-channel
    # as one speaker per channel. Prefer both channels when available.
    for key in ('recording_multi_channel_url', 'recording_url'):
        try:
            download_provider_asset(call.get(key), path)
        except MediaUnavailable:
            continue
        return
    raise MediaUnavailable


def download_provider_asset(url, path):
    if not isinstance(url, str):
        raise MediaUnavailable
    endpoint = urlsplit(url)
    if (endpoint.scheme != 'https' or endpoint.hostname not in settings.REPLAY_RETELL_AUDIO_HOSTS
            or endpoint.username or endpoint.password or endpoint.port not in (None, 443)):
        raise MediaUnavailable
    try:
        with requests.get(url, stream=True, allow_redirects=False, timeout=(4, 8)) as response, path.open('wb') as out:
            if response.status_code != 200:
                raise MediaUnavailable
            total = 0
            deadline = time.monotonic() + 70
            for data in response.iter_content(1024 * 1024):
                total += len(data)
                if total > storage.MAX_AUDIO or time.monotonic() > deadline:
                    raise MediaUnavailable
                out.write(data)
    except requests.RequestException:
        raise MediaUnavailable from None


def audio_anchor(call, audio):
    start, end = call.get('start_timestamp'), call.get('end_timestamp')
    if type(start) is not int or type(end) is not int or start <= 0:
        raise Unaligned
    _, first, duration = probe(audio, 'a:0')
    provider_duration = (end - start) / 1000
    if (not MIN_COVERAGE <= duration <= storage.MAX_CAPTURE + 10 or abs(first) > 0.1
            or abs(duration - provider_duration) > MAX_DRIFT):
        raise Unaligned
    # Provider word times share the audio timeline. If supplied, they must
    # fit through the last speaking turn before the replay can be published.
    turns = call.get('transcript_object')
    if isinstance(turns, list):
        ends = [word['end']
                for turn in turns if isinstance(turn, dict) and isinstance(turn.get('words'), list)
                for word in turn['words'] if isinstance(word, dict) and type(word.get('end')) in (int, float)]
        if ends and (not all(math.isfinite(value) for value in ends) or max(ends) > duration + 0.1):
            raise Unaligned
    return start, duration, round(duration - provider_duration, 3)


def collect_clips(replay, s3, directory, call_start):
    clips = []
    lost = False
    for row in replay.segments.order_by('start_ms', 'pk'):
        video = directory / f'{row.pk}.{row.mime.split("/")[1]}'
        last_chunk = None
        with video.open('wb') as out:
            for expected, obj in enumerate(row.chunks.filter(verified=True).order_by('sequence')):
                if obj.sequence != expected:
                    lost = True
                    break
                try:
                    storage.copy_chunk(s3, obj, out)
                except storage.InvalidChunk:
                    lost = True
                    break
                last_chunk = obj
        if not last_chunk:
            lost = True
            continue
        try:
            codec, first, duration = probe(video, 'v:0')
        except InvalidMedia:
            lost = True
            continue
        if codec not in ('vp8', 'vp9', 'h264', 'av1', 'hevc') or not 0 < duration <= storage.MAX_CAPTURE:
            lost = True
            continue
        complete = row.complete and last_chunk.sequence == row.next_sequence - 1
        end_ms = row.ended_ms if complete else last_chunk.elapsed_ms
        if end_ms is None:
            lost = True
            continue
        elapsed = end_ms / 1000
        drift = elapsed - duration - first
        if abs(first) > MAX_DRIFT or abs(drift) > MAX_DRIFT or elapsed <= first:
            lost = True
            continue
        uncertainty = row.uncertainty_ms / 1000
        clips.append(Clip(video, (row.start_ms - call_start) / 1000 + first,
                          duration, (elapsed - first) / duration, not complete,
                          drift, uncertainty))
    if not clips:
        raise Unaligned
    return clips, lost


def render(clips, duration, audio, output):
    inputs = []
    filters = []
    gaps = []
    cursor = 0.0
    quality_gap = False
    labels = []
    input_count = 0

    def append_gap(end):
        nonlocal cursor, quality_gap
        length = end - cursor
        if length <= 0:
            return
        label = f'part{len(labels)}'
        filters.append(f'color=black:s={WIDTH}x{HEIGHT}:r={FPS}:d={length:.6f}[{label}]')
        labels.append(f'[{label}]')
        gaps.append({'start': round(cursor, 3), 'end': round(end, 3)})
        quality_gap |= length > MAX_DRIFT
        cursor = end

    for clip in clips:
        start = max(cursor, clip.offset, 0)
        end = min(duration, clip.offset + clip.duration * clip.scale)
        if end - start < 1 / FPS:
            continue
        # Round only negligible holes to video frame precision. Larger holes
        # remain marked on the provider timeline without shifting audio.
        if start - cursor < 1 / FPS:
            start = cursor
        append_gap(start)
        index = input_count
        input_count += 1
        inputs.extend(['-threads', '1', *input_options(clip.source), '-i', str(clip.source)])
        trim = max(0, (start - clip.offset) / clip.scale)
        label = f'part{len(labels)}'
        filters.append(
            f'[{index}:v:0]setpts=PTS-STARTPTS,trim=start={trim:.6f},'
            f'setpts=(PTS-STARTPTS)*{clip.scale:.9f},'
            f'scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,'
            f'pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},'
            f'trim=duration={end-start:.6f},format=yuv420p[{label}]'
        )
        labels.append(f'[{label}]')
        quality_gap |= clip.incomplete
        cursor = end

    if duration - sum(gap['end'] - gap['start'] for gap in gaps) - (duration - cursor) < MIN_COVERAGE:
        raise Unaligned
    if duration - cursor >= 1 / FPS:
        append_gap(duration)

    filters.append(''.join(labels) + f'concat=n={len(labels)}:v=1:a=0[v]')
    run('ffmpeg', [
        '-nostdin', '-v', 'error', '-y', '-max_alloc', '134217728', *inputs,
        *input_options(audio), '-i', str(audio), '-filter_complex_threads', '2', '-filter_complex', ';'.join(filters),
        '-map', '[v]', '-map', f'{input_count}:a:0', '-t', f'{duration:.6f}',
        '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
        '-b:v', '450k', '-maxrate', '530k', '-bufsize', '1060k',
        '-c:a', 'aac', '-ac', '2', '-ar', '48000', '-b:a', '96k',
        '-movflags', '+faststart', '-fs', str(storage.MAX_OUTPUT), str(output),
    ], 540)
    return gaps, quality_gap


def validate(output, expected):
    video, video_start, video_length = probe(output, 'v:0')
    audio, audio_start, audio_length = probe(output, 'a:0')
    if (video != 'h264' or audio != 'aac' or abs(video_start) > 0.1 or abs(audio_start) > 0.1
            or abs(video_length - expected) > MAX_DRIFT or abs(audio_length - expected) > MAX_DRIFT
            or not 0 < output.stat().st_size < storage.MAX_OUTPUT):
        raise InvalidMedia
    with output.open('rb') as media:
        header = media.read(64 * 1024)
        # A large moov atom is fine. Its opening must precede media data.
        if header[4:8] != b'ftyp':
            raise InvalidMedia
        first_atom = int.from_bytes(header[:4], 'big')
        if header[first_atom + 4:first_atom + 8] != b'moov':
            raise InvalidMedia
    with output.open('rb') as media:
        return hashlib.file_digest(media, 'sha256').hexdigest()


def assemble(replay, call, s3, directory):
    audio = directory / 'retell.wav'
    download_audio(call, audio)
    start, duration, audio_drift = audio_anchor(call, audio)
    clips, lost = collect_clips(replay, s3, directory, start)
    output = directory / 'replay.mp4'
    gaps, partial = render(clips, duration, audio, output)
    digest = validate(output, duration)
    timing = {'retell_duration': round(duration, 3), 'retell_duration_error': audio_drift,
              'segments': [{'offset': round(clip.offset, 3), 'clock_uncertainty': clip.uncertainty,
                            'duration_error': round(clip.drift, 3)} for clip in clips]}
    return output, digest, gaps, partial or lost, timing
