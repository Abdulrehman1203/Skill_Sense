"""Small immutable results. No raw frames or landmark identities are persisted."""
from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class Calibration:
    horizontal: float
    vertical: float
    version: str = 'iris-baseline-v1'

    def __post_init__(self):
        if self.version != 'iris-baseline-v1' or any(
                type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                for v in (self.horizontal, self.vertical)):
            raise ValueError('Invalid gaze calibration.')

    @classmethod
    def from_dict(cls, value):
        if value is None or value == {}:
            return None
        if not isinstance(value, dict) or set(value) != {'horizontal', 'vertical', 'version'}:
            raise ValueError('Invalid gaze calibration schema.')
        return cls(**value)


@dataclass(frozen=True)
class Quality:
    usable: bool
    reason: str
    brightness: float
    contrast: float
    sharpness: float


@dataclass(frozen=True)
class Gaze:
    direction: str = 'unknown'
    reason: str = 'uncalibrated'
    horizontal: float | None = None
    vertical: float | None = None


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    box: tuple[float, float, float, float]

    def as_dict(self):
        return {'label': self.label, 'confidence': self.confidence, 'box': list(self.box)}


@dataclass(frozen=True)
class Observation:
    quality: Quality
    face_count: int | None
    gaze: Gaze
    objects: tuple[Detection, ...] = ()
    expression_status: str = 'disabled'

    def details(self, *, calibration=None, config_version, face_limit=3):
        return {'schema_version': 1, 'quality': asdict(self.quality),
                'face_count': self.face_count, 'face_limit': face_limit, 'gaze': asdict(self.gaze),
                'expression': {'status': self.expression_status, 'label': None},
                'calibration': asdict(calibration) if calibration else None,
                'config_version': config_version}
