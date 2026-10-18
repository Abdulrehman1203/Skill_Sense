"""Bounded offline CPU load experiment: real models, synthetic pixels, no accuracy claim.

This is NOT the PostgreSQL/Redis/Channels/Celery end-to-end acceptance test.
One runtime stays in its owning main thread. A producer holds only the latest
pending reference per simulated session; round-robin selection prevents starvation.
No outputs, image bytes, session IDs or credentials enter the report.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import platform
import threading
import time


def run(models, sessions, seconds, fps=3):
    import numpy as np
    import psutil
    from .evaluate import encode
    from .runtime import VisionRuntime
    from .measurements import percentiles
    image = encode(np.random.default_rng(812).integers(0, 256, (480, 640, 3), dtype=np.uint8))
    process = psutil.Process()
    baseline = process.memory_info().rss
    cold_start = time.perf_counter()
    runtime = VisionRuntime(models)
    cold = time.perf_counter()-cold_start
    lock, done = threading.Lock(), threading.Event()
    pending, counts = {}, Counter()
    service, end_to_end, per_session = [], [], [0]*sessions
    start = time.perf_counter()
    cpu_start = sum(process.cpu_times()[:2])
    peak, max_pending, oldest = baseline, 0, 0.

    def produce():
        for tick in range(round(seconds*fps)):
            deadline = start+tick/fps
            if done.wait(max(0, deadline-time.perf_counter())):
                return
            with lock:
                for session in range(sessions):
                    counts['offered'] += 1
                    if session in pending:
                        counts['superseded'] += 1
                    pending[session] = time.perf_counter()
        done.set()

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    cursor = 0
    try:
        while not done.is_set() or pending:
            with lock:
                max_pending = max(max_pending, len(pending))
                chosen = next((s for offset in range(sessions) if (s := (cursor+offset) % sessions) in pending), None)
                received = pending.pop(chosen) if chosen is not None else None
                if chosen is not None:
                    cursor = (chosen+1) % sessions
            if received is None:
                time.sleep(.001)
                continue
            tick = time.perf_counter()
            oldest = max(oldest, tick-received)
            runtime.analyze(image, None)  # No target calibration or real person is supplied.
            end = time.perf_counter()
            service.append((end-tick)*1000)
            end_to_end.append((end-received)*1000)
            per_session[chosen] += 1
            peak = max(peak, process.memory_info().rss)
        elapsed = time.perf_counter()-start
        return {'schema_version': 1, 'scope': 'offline_real_models_synthetic_pixels_only',
            'accuracy_validation': 'outstanding', 'platform': platform.platform(),
            'device': 'CPUExecutionProvider',
            'precision': 'YOLO ONNX FP32; MediaPipe pinned task internal precision not profiled',
            'model_version': runtime.model_version, 'config_version': runtime.config.version,
            'resolution': [640, 480], 'jpeg_bytes': len(image), 'sessions': sessions, 'fps_per_session': fps,
            'offering_seconds': seconds, 'elapsed_including_drain_seconds': round(elapsed, 3),
            'cold_load_warmup_seconds': round(cold, 3), 'offered': counts['offered'],
            'processed': len(service), 'superseded': counts['superseded'], 'failed': 0,
            'offered_per_second': round(counts['offered']/seconds, 3),
            'processed_per_second': round(len(service)/elapsed, 3),
            'max_pending': max_pending, 'pending_bound': sessions,
            'oldest_selected_age_seconds': round(oldest, 3),
            'processing_ms': percentiles(service), 'end_to_end_ms': percentiles(end_to_end),
            'processed_per_simulated_session': per_session,
            'fairness_min_over_max': round(min(per_session)/max(per_session), 4),
            'rss_baseline_bytes': baseline, 'sampled_peak_rss_bytes': peak,
            'cpu_one_core_percent': round(100*(sum(process.cpu_times()[:2])-cpu_start)/elapsed, 2),
            'gpu_vram_bytes': None, 'cleanup_failures': None, 'general_queue_latency_ms': None,
            'budget': {'end_to_end_p95_ms': 500, 'max_loss_fraction': .05},
            'meets_offline_budget': end_to_end and percentiles(end_to_end)['p95'] <= 500
                and counts['superseded']/counts['offered'] <= .05}
    finally:
        done.set()
        producer.join(timeout=2)
        runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', required=True)
    parser.add_argument('--sessions', type=int, choices=range(1, 17), required=True)
    parser.add_argument('--seconds', type=int, choices=range(1, 61), default=20)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    result = run(args.models, args.sessions, args.seconds)
    Path(args.report).write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
