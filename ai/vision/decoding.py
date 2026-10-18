"""Strict bounded JPEG/quality adapter; imports no ML runtime and no Django."""
from io import BytesIO
import warnings


def decode_jpeg(data, *, max_bytes=204800, max_pixels=921600, max_edge=1280):
    from PIL import Image
    import numpy as np
    if not isinstance(data, bytes) or not 4 <= len(data) <= max_bytes:
        raise ValueError('Invalid JPEG size.')
    if not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
        raise ValueError('Invalid JPEG envelope.')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as image:
            if image.format != 'JPEG' or image.width > max_edge or image.height > max_edge or image.width*image.height > max_pixels:
                raise ValueError('JPEG dimensions exceed policy.')
            if min(image.size) < 64:
                raise ValueError('JPEG dimensions too small.')
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
            return np.asarray(image.convert('RGB')).copy()


def quality(rgb):
    import numpy as np
    from .types import Quality
    gray = rgb.astype(np.float32).mean(axis=2)
    brightness = float(gray.mean())
    contrast = float(gray.std())
    laplace = (4*gray[1:-1, 1:-1]-gray[:-2, 1:-1]-gray[2:, 1:-1]
               -gray[1:-1, :-2]-gray[1:-1, 2:])
    sharpness = float(laplace.var())
    reason = ('too_dark' if brightness < 35 else 'too_bright' if brightness > 225
              else 'low_contrast' if contrast < 12 else 'blurred' if sharpness < 25 else 'usable')
    return Quality(reason == 'usable', reason, round(brightness, 4), round(contrast, 4), round(sharpness, 4))
