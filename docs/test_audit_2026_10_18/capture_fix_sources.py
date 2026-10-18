"""Hash application/test sources only; never inspect environment secrets."""
import hashlib
import json
from pathlib import Path
import sys

here = Path(__file__).resolve().parent
back = here.parents[1]
front = Path('D:/Programming/FYP/Skill_Sense_frontend')
files = []
for folder in ('ai', 'ai_interview_recruitment', 'interview_system'):
    files.extend(p for p in (back/folder).rglob('*.py') if '__pycache__' not in p.parts)
for folder in ('lib', 'test', 'tooling'):
    files.extend(p for p in (front/folder).rglob('*') if p.is_file()
                 and p.suffix in {'.dart', '.js', '.mjs'} and 'node_modules' not in p.parts)
files.extend([front/'pubspec.yaml', front/'pubspec.lock', back/'requirements.txt'])
manifest = {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(files))}
mode = sys.argv[1]
target = here/f'fix-sources-{mode}.json'
target.write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
if mode == 'after':
    before = json.loads((here/'fix-sources-before.json').read_text())
    changes = [p for p in sorted(before.keys() | manifest.keys()) if before.get(p) != manifest.get(p)]
    (here/'fix-source-drift.json').write_text(json.dumps(changes, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'files': len(manifest), 'changed': changes}))
else:
    print(f'Captured {len(manifest)} source/test/configuration hashes.')
