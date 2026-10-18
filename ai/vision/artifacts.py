"""Explicit operator artifact fetch. Inference never downloads weights."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory')
    parser.add_argument('--export-yolo', action='store_true')
    args = parser.parse_args()
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(Path(__file__).with_name('model_manifest.json').read_text())
    for name, spec in manifest.items():
        path = directory/name
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != spec['sha256']:
                raise ValueError('Existing artifact checksum mismatch; no overwrite performed.')
            continue
        if spec['url'] is None:
            if not args.export_yolo:
                raise ValueError('Provide the pinned YOLO ONNX artifact, or build it with --export-yolo in the separate export environment.')
            source = spec['source']
            pt = directory/'yolov8n.pt'
            if not pt.exists():
                with urlopen(source['url'], timeout=60) as response:
                    payload = response.read(source['bytes']+1)
                if len(payload) != source['bytes'] or hashlib.sha256(payload).hexdigest() != source['sha256']:
                    raise ValueError('YOLO source checksum mismatch.')
                with pt.open('xb') as target:
                    target.write(payload)
            if hashlib.sha256(pt.read_bytes()).hexdigest() != source['sha256']:
                raise ValueError('YOLO source checksum mismatch.')
            import os
            os.environ['YOLO_AUTOINSTALL'] = 'false'
            os.environ['YOLO_CONFIG_DIR'] = str(directory.resolve()/'.settings')
            from ultralytics import YOLO, settings
            settings.update({'sync': False})
            YOLO(str(pt)).export(format='onnx', opset=12, simplify=False, dynamic=False,
                                 imgsz=640, batch=1, device='cpu')
            import onnx
            model = onnx.load(str(path))
            metadata = {x.key: x.value for x in model.metadata_props if x.key != 'date'}
            del model.metadata_props[:]
            onnx.helper.set_model_props(model, dict(sorted(metadata.items())))
            data = model.SerializeToString(deterministic=True)
            # Remove volatile build date before hashing. Never accept a new hash
            # silently if another exporter/platform produces different bytes.
            if len(data) != spec['bytes'] or hashlib.sha256(data).hexdigest() != spec['sha256']:
                raise ValueError('Export differs from pinned artifact; review the toolchain instead of changing hashes at runtime.')
            path.write_bytes(data)
            continue
        with urlopen(spec['url'], timeout=60) as source:
            data = source.read(spec['bytes']+1)
        if len(data) != spec['bytes'] or hashlib.sha256(data).hexdigest() != spec['sha256']:
            raise ValueError('Downloaded artifact checksum mismatch.')
        with path.open('xb') as target:
            target.write(data)
    print('Pinned local model artifacts verified.')


if __name__ == '__main__':
    main()
