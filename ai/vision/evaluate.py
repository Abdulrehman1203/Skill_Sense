"""Offline bounded fixture/benchmark runner. Never downloads or saves raw frames."""
import argparse
from collections import defaultdict
import importlib.metadata
from io import BytesIO
import json
import os
from pathlib import Path
import platform
import statistics
import threading
import time


def encode(rgb):
    from PIL import Image
    output = BytesIO()
    Image.fromarray(rgb).save(output, format='JPEG', quality=65)
    return output.getvalue()


def synthetic():
    import numpy as np
    rng = np.random.default_rng(812)
    for index in range(15):
        kind = ('dark', 'flat', 'noise')[index % 3]
        rgb = (np.zeros((480, 640, 3), dtype=np.uint8) if kind == 'dark' else
               np.full((480, 640, 3), 128, dtype=np.uint8) if kind == 'flat' else
               rng.integers(0, 256, (480, 640, 3), dtype=np.uint8))
        yield {'fixture': kind, 'seconds': index/3, 'jpeg': encode(rgb)}


def real_frames(manifest_path):
    """Only explicitly local, consent-attested fixtures. No network sources."""
    import cv2
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('consented_staged') is not True or manifest.get('schema_version') != 1:
        raise ValueError('A local consented/staged fixture manifest is required.')
    total = 0
    for clip in manifest['clips']:
        path = (manifest_path.parent/clip['path']).resolve()
        if not path.is_relative_to(manifest_path.parent) or not path.is_file():
            raise ValueError('Fixture is missing or outside the local fixture directory.')
        from hashlib import file_digest
        with path.open('rb') as source:
            if file_digest(source, 'sha256').hexdigest() != clip['sha256']:
                raise ValueError('Fixture checksum mismatch.')
        capture = cv2.VideoCapture(str(path))
        try:
            fps = capture.get(cv2.CAP_PROP_FPS)
            width, height = capture.get(cv2.CAP_PROP_FRAME_WIDTH), capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
            if not capture.isOpened() or not 1 <= fps <= 120:
                raise ValueError('Invalid clip frame rate.')
            if not 64 <= width <= 1920 or not 64 <= height <= 1080:
                raise ValueError('Fixture dimensions exceed the offline evaluation policy.')
            frame_no, stride = 0, max(1, round(fps/3))
            while total < 1800:
                okay, bgr = capture.read()
                if not okay:
                    break
                frame_no += 1
                if frame_no % stride:
                    continue
                height, width = bgr.shape[:2]
                factor = min(1, 1280/width, 1280/height, (921600/(width*height))**0.5)
                if factor < 1:
                    bgr = cv2.resize(bgr, (int(width*factor), int(height*factor)))
                total += 1
                yield {'fixture': clip['path'], 'seconds': (frame_no-1)/fps,
                       'jpeg': encode(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)),
                       'calibration': clip.get('calibration')}
        finally:
            capture.release()
        if total >= 1800:
            raise ValueError('Fixture run exceeds 1800 sampled-frame bound; split the manifest.')


def run(directory, samples, *, synthetic_only):
    import psutil
    from .runtime import VisionRuntime
    from .types import Calibration
    process = psutil.Process()
    stop = threading.Event()
    baseline = process.memory_info().rss
    peak = [baseline]
    def monitor():
        while not stop.wait(.02):
            peak[0] = max(peak[0], process.memory_info().rss)
    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    runtime = None
    outcomes, latencies = [], defaultdict(list)
    started = time.perf_counter()
    try:
        runtime = VisionRuntime(directory)
        cold = time.perf_counter()-started
        for index, sample in enumerate(samples):
            if index >= 1800:
                raise ValueError('Frame count exceeds benchmark bound.')
            tick = time.perf_counter()
            baseline_gaze = Calibration.from_dict(sample.get('calibration'))
            observation = runtime.analyze(sample['jpeg'], baseline_gaze)
            latency = (time.perf_counter()-tick)*1000
            latencies[sample['fixture']].append(latency)
            outcomes.append({'fixture': sample['fixture'], 'seconds': sample['seconds'],
                'vision_details': observation.details(calibration=baseline_gaze, config_version=runtime.config.version,
                                                     face_limit=runtime.config.max_faces),
                'detected_objects': [item.as_dict() for item in observation.objects]})
        peak[0] = max(peak[0], process.memory_info().rss)
        return {'schema_version': 1, 'synthetic_only': synthetic_only,
            'accuracy_validation': 'outstanding' if synthetic_only else 'observations_for_human_review',
            'environment': {'python': platform.python_version(), 'platform': platform.platform(),
                'processor': os.environ.get('PROCESSOR_IDENTIFIER', platform.processor()), 'device': 'cpu',
                'packages': {name: importlib.metadata.version(name) for name in
                    ('mediapipe', 'onnxruntime', 'numpy', 'opencv-contrib-python')}},
            'model_version': runtime.model_version, 'config_version': runtime.config.version,
            'cold_load_and_warmup_seconds': round(cold, 4),
            'rss_baseline_bytes': baseline, 'sampled_process_peak_rss_bytes': peak[0],
            'latency_ms': {name: {'count': len(values), 'median': round(statistics.median(values), 4),
                                'max': round(max(values), 4)} for name, values in latencies.items()},
            'observations': outcomes, 'gpu_measurement': 'not_performed'}
    finally:
        if runtime:
            runtime.close()
        stop.set(); watcher.join(timeout=1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--models', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--synthetic', action='store_true')
    group.add_argument('--manifest')
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    report = run(args.models, synthetic() if args.synthetic else real_frames(args.manifest),
                 synthetic_only=args.synthetic)
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'observations'}, indent=2))


if __name__ == '__main__':
    main()
