"""Bounded JPEG validation only. No model imports, inference or image persistence."""
import asyncio
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
import json
import math
from threading import BoundedSemaphore

from django.conf import settings
from PIL import Image, UnidentifiedImageError


class ProtocolError(Exception):
    def __init__(self, reason, code=1008):
        self.reason = reason
        self.code = code
        super().__init__(reason)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field.')
        result[key] = value
    return result


def parse_message(text):
    try:
        message = json.loads(text, object_pairs_hook=_unique_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        raise ProtocolError('invalid_message') from None
    if not isinstance(message, dict):
        raise ProtocolError('invalid_message')
    if message == {'type': 'ping'}:
        return message
    if set(message) != {'type', 'protocol_version', 'sequence', 'capture_ts', 'data'} or message['type'] != 'frame':
        raise ProtocolError('invalid_message')
    if type(message['protocol_version']) is not int or message['protocol_version'] != 1:
        raise ProtocolError('unsupported_version')
    sequence = message['sequence']
    if type(sequence) is not int or not 0 <= sequence <= 9007199254740991:
        raise ProtocolError('invalid_sequence')
    capture_ts = message['capture_ts']
    if type(capture_ts) not in (int, float) or not math.isfinite(capture_ts):
        raise ProtocolError('invalid_frame')
    data = message['data']
    if not isinstance(data, str) or not data or len(data) > 4 * ((settings.INTERVIEW_ANALYSIS_MAX_JPEG_BYTES + 2) // 3):
        raise ProtocolError('invalid_frame')
    return message


@dataclass(frozen=True, repr=False)
class ValidatedJPEG:
    data: bytes
    width: int
    height: int


def decode_jpeg(encoded, limits):
    max_bytes, max_width, max_height, max_pixels = limits
    try:
        jpeg = base64.b64decode(encoded, validate=True)
        if (not jpeg or len(jpeg) > max_bytes or base64.b64encode(jpeg).decode('ascii') != encoded
                or not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9')):
            raise ProtocolError('invalid_frame')
        with Image.open(BytesIO(jpeg), formats=['JPEG']) as image:
            width, height = image.size
            if not (0 < width <= max_width and 0 < height <= max_height and width * height <= max_pixels):
                raise ProtocolError('dimensions_exceeded')
            image.verify()
        with Image.open(BytesIO(jpeg), formats=['JPEG']) as image:
            image.load()  # verify() alone does not decode the raster or detect all truncation.
        return ValidatedJPEG(jpeg, width, height)
    except Image.DecompressionBombError:
        raise ProtocolError('dimensions_exceeded') from None
    except (ValueError, binascii.Error, OSError, SyntaxError, UnidentifiedImageError):
        raise ProtocolError('invalid_frame') from None


class DecoderPool:
    def __init__(self, workers):
        self.slots = BoundedSemaphore(max(1, workers))
        self.executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix='interview-analysis-jpeg')

    async def decode(self, encoded):
        if not self.slots.acquire(blocking=False):
            return None  # No queued decode jobs, including after a waiter times out.
        limits = (settings.INTERVIEW_ANALYSIS_MAX_JPEG_BYTES, settings.INTERVIEW_ANALYSIS_MAX_WIDTH,
                  settings.INTERVIEW_ANALYSIS_MAX_HEIGHT, settings.INTERVIEW_ANALYSIS_MAX_PIXELS)
        try:
            future = self.executor.submit(decode_jpeg, encoded, limits)
        except BaseException:
            self.slots.release()
            raise
        future.add_done_callback(lambda _: self.slots.release())
        # Cancellation cannot free a still-running decoder's slot early.
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), settings.INTERVIEW_ANALYSIS_DECODE_TIMEOUT_SECONDS)
        except TimeoutError:
            return None


_pool = None


def get_decoder():
    global _pool
    if _pool is None:
        _pool = DecoderPool(settings.INTERVIEW_ANALYSIS_DECODE_WORKERS)
    return _pool
