"""Worker-owned IMAGE-mode resources. No initialization on module import.

One instance per execution process; serialized access, no YOLO tracking and no
MediaPipe video history. Forked instances are rejected; close before shutdown.
"""
from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
import threading

from .decoding import decode_jpeg, quality
from .gaze import estimate
from .types import Detection, Gaze, Observation


@dataclass(frozen=True)
class Config:
    max_faces: int = 3
    face_detection_confidence: float = 0.6
    face_presence_confidence: float = 0.6
    object_confidence: float = 0.4
    nms_iou: float = 0.7
    image_size: int = 640
    threads: int = 2
    device: str = 'cpu'

    def __post_init__(self):
        if (type(self.max_faces) is not int or not 2 <= self.max_faces <= 5
                or self.device != 'cpu' or not 1 <= self.threads <= 4 or self.image_size != 640
                or any(not 0.1 <= v <= 0.95 for v in (self.face_detection_confidence,
                         self.face_presence_confidence, self.object_confidence, self.nms_iou))):
            raise ValueError('Unsupported vision configuration.')

    @property
    def version(self):
        return 'config-v1-'+hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


def verified_artifacts(directory):
    manifest = json.loads(Path(__file__).with_name('model_manifest.json').read_text())
    result = {}
    for name, spec in manifest.items():
        path = Path(directory, name)
        if not path.is_file() or path.stat().st_size != spec['bytes']:
            raise ValueError('Missing or incorrect vision artifact.')
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        if digest != spec['sha256']:
            raise ValueError('Vision artifact checksum mismatch.')
        result[name] = path.resolve()
    return result, manifest


def normalize_detections(boxes, names, width, height):
    """YOLO xyxy pixels -> normalized xywh, retain reported confidence."""
    import math
    detections = []
    for xyxy, confidence, class_id in boxes:
        label = str(names[int(class_id)]).strip().lower().replace('_', ' ')
        label = 'phone' if label in ('cell phone', 'cellphone', 'mobile phone', 'phone') else label
        if label not in ('person', 'phone'):
            continue
        values = [float(v) for v in xyxy] + [float(confidence)]
        if len(values) != 5 or not all(math.isfinite(v) for v in values) or not 0 <= values[4] <= 1:
            raise ValueError('Invalid detector output.')
        x0, y0, x1, y1 = values[:4]
        x0, x1 = max(0., min(1., x0/width)), max(0., min(1., x1/width))
        y0, y1 = max(0., min(1., y0/height)), max(0., min(1., y1/height))
        if x1 <= x0 or y1 <= y0:
            continue
        detections.append(Detection(label, values[4], (x0, y0, x1-x0, y1-y0)))
    return tuple(sorted(detections, key=lambda d: (-d.confidence, d.label, d.box))[:20])


class VisionRuntime:
    def __init__(self, directory, config=None):
        self.config = config or Config()
        self.pid = os.getpid()
        self.owner = threading.get_ident()
        self.lock = threading.Lock()
        self.ready = False
        self.face = None
        self.detector = None
        paths, manifest = verified_artifacts(directory)  # Before any native model parser.
        self.model_version = 'mp1-'+manifest['face_landmarker.task']['sha256'][:12]+'-y8nonnx-'+manifest['yolov8n.onnx']['sha256'][:12]
        try:
            from importlib.metadata import version
            for name, expected in {'mediapipe': '0.10.21', 'onnxruntime': '1.20.1',
                                   'numpy': '1.26.4', 'opencv-contrib-python': '4.11.0.86'}.items():
                if version(name) != expected:
                    raise ValueError('Vision runtime does not match the validated dependency pins.')
            import onnxruntime as ort
            import mediapipe as mp
            from mediapipe.tasks.python import vision
            self.mp = mp
            self.face = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
                base_options=mp.tasks.BaseOptions(model_asset_path=str(paths['face_landmarker.task']),
                                                delegate=mp.tasks.BaseOptions.Delegate.CPU),
                running_mode=vision.RunningMode.IMAGE, num_faces=self.config.max_faces,
                min_face_detection_confidence=self.config.face_detection_confidence,
                min_face_presence_confidence=self.config.face_presence_confidence,
                output_face_blendshapes=False, output_facial_transformation_matrixes=True))
            options = ort.SessionOptions()
            options.intra_op_num_threads = self.config.threads
            options.inter_op_num_threads = 1
            self.detector = ort.InferenceSession(str(paths['yolov8n.onnx']), sess_options=options,
                                                providers=['CPUExecutionProvider'])
            self.detector.disable_fallback()
            if (self.detector.get_inputs()[0].shape != [1, 3, 640, 640]
                    or self.detector.get_outputs()[0].shape != [1, 84, 8400]):
                raise ValueError('Unsupported YOLO export shape.')
            # Explicit warm-up bypasses quality rejection so BOTH models execute.
            import numpy as np
            self._models(np.zeros((640, 640, 3), dtype=np.uint8), None)
            self.ready = True
        except BaseException:
            self.close()
            raise

    def _models(self, rgb, calibration):
        import numpy as np
        faces = self.face.detect(self.mp.Image(image_format=self.mp.ImageFormat.SRGB,
                                               data=np.ascontiguousarray(rgb)))
        from .yolo import prepare, postprocess
        tensor, transform = prepare(rgb)
        output = self.detector.run(None, {self.detector.get_inputs()[0].name: tensor})[0]
        boxes = postprocess(output, transform, self.config.object_confidence, self.config.nms_iou)
        objects = normalize_detections(boxes, {0: 'person', 67: 'cell phone'}, rgb.shape[1], rgb.shape[0])
        matrices = faces.facial_transformation_matrixes
        gaze = estimate(faces.face_landmarks, matrices[0] if len(matrices) == 1 else None, calibration)
        return len(faces.face_landmarks), gaze, objects

    def analyze(self, data, calibration=None):
        if os.getpid() != self.pid or threading.get_ident() != self.owner:
            raise RuntimeError('Vision runtime used outside its owning execution context.')
        if not self.ready:
            raise RuntimeError('Vision runtime is not ready.')
        with self.lock:
            rgb = decode_jpeg(data)
            assessment = quality(rgb)
            if not assessment.usable:
                return Observation(assessment, None, Gaze(reason='quality_unusable'))
            count, gaze, detections = self._models(rgb, calibration)
            return Observation(assessment, count, gaze, detections)

    def close(self):
        self.ready = False
        if self.face is not None:
            self.face.close()
            self.face = None
        self.detector = None
