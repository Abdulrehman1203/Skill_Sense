"""Worker-only B5 boundary. Importing this module loads no ML runtimes."""
import json
import os
import uuid


class InferenceUnavailable(Exception):
    pass


class TransientInferenceError(Exception):
    """B5 may raise for a bounded, retryable local resource failure."""


_runtime = None
_owner = None


def _context():
    from celery import current_task
    task = current_task._get_current_object()
    if (os.environ.get('INTERVIEW_ANALYSIS_VISION_PROCESS') != '1' or task is None
            or task.name not in ('interview_system.tasks.vision.process_frame',
                                         'interview_system.tasks.vision.warm_vision')
            or task.request.is_eager):
        raise InferenceUnavailable('Vision is restricted to the dedicated execution process.')


def ensure_warm():
    global _runtime, _owner
    _context()
    from django.conf import settings
    if not settings.INTERVIEW_ANALYSIS_VISION_ENABLED:
        raise InferenceUnavailable('Vision is disabled.')
    if _runtime is None:
        from ai.vision.runtime import Config, VisionRuntime
        _runtime = VisionRuntime(settings.INTERVIEW_ANALYSIS_MODEL_DIRECTORY,
                                Config(max_faces=settings.INTERVIEW_ANALYSIS_MAX_FACES))
        _owner = uuid.uuid4().hex
    from .vision_payloads import PayloadStore
    store = PayloadStore()
    try:
        store.client.set(store.prefix+':ready', json.dumps({'owner': _owner,
            'model_version': _runtime.model_version, 'config_version': _runtime.config.version}), ex=15)
    finally:
        store.close()
    return _runtime


def worker_ready():
    """Readiness is a short-lived warm-process lease, not proof of accuracy/SLA."""
    from django.conf import settings
    from .vision_payloads import PayloadStore
    store = PayloadStore()
    try:
        with store.client.pipeline() as pipe:
            raw, ttl = pipe.get(store.prefix+':ready').pttl(store.prefix+':ready').execute()
        if not raw or len(raw) > 1024 or not 0 < ttl <= 15000:
            return False
        try:
            value = json.loads(raw)
            from ai.vision.runtime import Config
            from pathlib import Path
            manifest = json.loads((Path(__file__).parent.parent/'ai/vision/model_manifest.json').read_text())
            expected = 'mp1-'+manifest['face_landmarker.task']['sha256'][:12]+'-y8nonnx-'+manifest['yolov8n.onnx']['sha256'][:12]
            return (set(value) == {'owner', 'model_version', 'config_version'} and
                    value['model_version'] == expected and isinstance(value['owner'], str) and len(value['owner']) == 32
                    and value['config_version'] == Config(max_faces=settings.INTERVIEW_ANALYSIS_MAX_FACES).version)
        except (ValueError, TypeError, KeyError):
            return False
    finally:
        store.close()


def close_runtime(**kwargs):
    global _runtime, _owner
    if _runtime is not None:
        _runtime.close()
        _runtime = None
    if _owner:
        from .vision_payloads import PayloadStore
        store = PayloadStore()
        try:
            # A dead worker cannot delete another worker's newer readiness lease.
            store.client.eval("local v=redis.call('GET',KEYS[1]); if v and cjson.decode(v).owner==ARGV[1] then return redis.call('DEL',KEYS[1]) end; return 0",
                              1, store.prefix+':ready', _owner)
        except Exception:
            pass
        finally:
            store.close()
        _owner = None


def analyze_jpeg(data, *, calibration=None):
    runtime = ensure_warm()
    from ai.vision.types import Calibration
    from ai.vision.rules import RuleConfig
    baseline = Calibration.from_dict(calibration)
    try:
        observation = runtime.analyze(data, baseline)
    except Exception:
        close_runtime()
        raise
    return {'gaze_direction': observation.gaze.direction,
            'face_count': observation.face_count,
            'detected_objects': [item.as_dict() for item in observation.objects],
            'vision_details': observation.details(calibration=baseline, config_version=runtime.config.version,
                                                   face_limit=runtime.config.max_faces)}, {
                'pipeline_version': 'vision-v1', 'model_version': runtime.model_version,
                'rules_version': RuleConfig().version}
