"""Pinned YOLOv8n ONNX adapter: RGB letterbox, class-aware bounded NMS.

The fixed export is float32 [1,3,640,640] -> [1,84,8400], COCO80 xywh plus
class probabilities (no separate objectness column). No tracker/history.
"""


def prepare(rgb):
    import cv2
    import numpy as np
    height, width = rgb.shape[:2]
    scale = min(640/width, 640/height)
    resized_width, resized_height = round(width*scale), round(height*scale)
    left, top = (640-resized_width)//2, (640-resized_height)//2
    image = np.full((640, 640, 3), 114, dtype=np.uint8)
    image[top:top+resized_height, left:left+resized_width] = cv2.resize(rgb, (resized_width, resized_height))
    return np.ascontiguousarray(image.transpose(2, 0, 1)[None], dtype=np.float32)/255., (scale, left, top)


def postprocess(output, transform, confidence=.4, iou_threshold=.7):
    import numpy as np
    if output.shape != (1, 84, 8400) or not np.isfinite(output).all():
        raise ValueError('Unexpected YOLO tensor schema.')
    values = output[0].T
    classes = values[:, 4:].argmax(axis=1)
    scores = values[np.arange(len(values)), classes+4]
    mask = (scores >= confidence) & np.isin(classes, [0, 67])
    values, classes, scores = values[mask, :4], classes[mask], scores[mask]
    if not len(values):
        return []
    # Bound NMS to the best 300 proposals, deterministic ties by original index.
    order = np.argsort(-scores, kind='stable')[:300]
    xyxy = np.concatenate((values[:, :2]-values[:, 2:]/2, values[:, :2]+values[:, 2:]/2), axis=1)
    selected = []
    while len(order) and len(selected) < 20:
        index, remaining = int(order[0]), order[1:]
        selected.append(index)
        if not len(remaining):
            break
        intersection = (np.maximum(0, np.minimum(xyxy[index, 2:], xyxy[remaining, 2:])
                                      -np.maximum(xyxy[index, :2], xyxy[remaining, :2]))).prod(axis=1)
        area = np.maximum(0, xyxy[:, 2:]-xyxy[:, :2]).prod(axis=1)
        overlap = intersection/np.maximum(area[index]+area[remaining]-intersection, 1e-9)
        order = remaining[(classes[remaining] != classes[index]) | (overlap <= iou_threshold)]
    scale, left, top = transform
    boxes = []
    for index in selected:
        coords = (xyxy[index]-np.array([left, top, left, top]))/scale
        boxes.append((coords.tolist(), float(scores[index]), int(classes[index])))
    return boxes
