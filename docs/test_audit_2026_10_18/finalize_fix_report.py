"""Summarize final evidence without rewriting the original audit."""
from collections import Counter
import json
from pathlib import Path

here = Path(__file__).resolve().parent
back = here.parents[1]
def read(name):
    return json.loads((here/name).read_text(encoding='utf-8-sig'))
def link(name, label=None):
    return f'[{label or name}](test_audit_2026_10_18/{name})'

backend = read('fix-backend-verified.json')
front = read('fix-frontend-sequential.json')
browser = read('fix-browser-unit-final.json')
baseline = read('backend.json')
assert backend['exit_code'] == 0 and not backend['failures'] and not backend['errors']
assert front['success'] is True
assert browser['success'] is True and browser['counts']=={'success':14}
counts = Counter(t['status'] for t in backend['tests'])
rows = [
    ('Full backend discovery', f"{backend['ran']} passed; 0 failures/errors/skips; 301.597s tests / {backend['seconds']}s including setup/teardown", 'fix-backend-verified.json'),
    ('Standalone scoring namespace', '10 passed; 0.015s', 'fix-scoring-unit.log'),
    ('Full sequential Flutter VM suite', f"{front['counts'].get('success', 0)} passed; {front['counts'].get('skipped', 0)} browser-only skip; 0 failures; {front['seconds']}s", 'fix-frontend-sequential.json'),
    ('Browser client-hints + OAuth unit fixtures', f"14 passed; 0 failures/skips; {browser['seconds']}s; test channel bridge / fake Clerk, real browser navigator", 'fix-browser-unit-final.json'),
    ('Full Flutter analyzer', 'No issues found; exit 0; 8.9s', 'fix-flutter-analyze-final-verified.log'),
    ('Node voice SDK bridge', '5 passed; 0 failed; fixture only; 327.5411ms', 'fix-voice-sdk-node-confirmed.log'),
    ('Headless Chrome compiled auth smoke', '2 passed; exit 0; fake Clerk fixture, no live provider/media', 'fix-browser-auth-smoke.json'),
    ('Migration consistency', 'No changes detected; no new migration', 'fix-migration-verified.log'),
    ('OpenAPI schema generation/validation', 'Exit 0; no warnings/errors', 'fix-api-schema-verified.log'),
    ('Flutter lockfile', 'enforce-lockfile succeeded', 'fix-lockfile.log'),
    ('Development deployment check', '6 security warnings; not a production acceptance result', 'fix-deploy-check.log'),
    ('Strict local Interview Analysis readiness', 'Exit 1: pending application migrations and noncompliant shared/persistent payload Redis; vision remains disabled', 'fix-readiness-verified.log'),
]
text = ['| Check | Exact result | Evidence |', '|---|---|---|']
text += [f'| {name} | {result} | {link(file)} |' for name,result,file in rows]
text += ['', 'Additional overlapping confirmations: real Redis notification tests **3/3 passed** under both minimal and original audit routing; Clerk deactivation/admin race subset **27/27 passed**; overload/actual-wire follow-up **2/2 passed**. These counts are not added to the full-run total.', '',
         'The intermediate 548-test run had 546 passes, one expired test publisher and one cold-start timeout. Both were fixed at test boundaries and are superseded by the complete passing run above. The intermediate Flutter asset-conflict run is likewise superseded by the sequential passing run. Initial Chrome/Wasm loading attempts stalled; the Windows CanvasKit workaround subsequently enabled test execution. An initial client-hints timeout was fixed with the unit-test channel bridge; all 14 browser cases then passed. The compiled auth smoke is a separate, overlapping result.', '',
         'Source checks captured 352 source/test/configuration files. Backend application and test sources remained unchanged during the final full backend run. Four frontend browser-test/tooling files were added/changed during verification and were checked by the final 14-case browser run and final analyzer. This does not certify later concurrent edits.', '',
         'Reproduction commands (backend from backend root, Flutter/Node from frontend root):', '', '```powershell',
         "$env:AUDIT_REPORT_NAME='repeat-backend'", '.venv/Scripts/python.exe docs/test_audit_2026_10_18/run_backend.py',
         '.venv/Scripts/python.exe -m unittest ai.scoring.test_rubric -v',
         'flutter pub get --enforce-lockfile', 'flutter test --no-pub --reporter json', 'flutter analyze --no-pub',
         'node --test tooling/voice_sdk/bridge.test.js',
         "$env:FLUTTER_ROOT='D:/Programming/flutter_windows_3.44.4-stable/flutter'",
         'node tooling/browser_qa/run_flutter_tests.mjs', '```', '',
         'The isolated backend runner requires local PostgreSQL/Redis access and blocks non-loopback provider calls. Do not run multiple Flutter test/build commands against the same output directory concurrently.', '',
         '### Original backend failure-event reconciliation', '',
         'Every original failed/error event below maps to the final discovered test method. Parenthesized subtest parameters share their parent method result. Fixtures and assertions were migrated to actual current contracts where described above; this does not restore removed fake output or automatic shortlisting.', '',
         '| Original failing/error test | Final result |', '|---|---|']
passed = {t['id']: t['status'] for t in backend['tests']}
renamed = {
    'interview_system.test_screening.ScreeningTests.test_task_shortlists_without_final_scoring_rubric':
        'interview_system.test_screening.ScreeningTests.test_task_recommends_without_automatically_shortlisting',
    'interview_system.tests.test_phase9_webhook.RetellWebhookTests.test_queue_failure_returns_503_and_video_hook_remains_independent':
        'interview_system.tests.test_phase9_webhook.RetellWebhookTests.test_queue_failure_retains_durable_event_and_video_hook_remains_independent',
}
reconciliation=[]
for kind in ('failures','errors'):
    for item in baseline[kind]:
        original=item['id']
        key=original.split(' (')[0]
        key=renamed.get(key, key)
        status=passed.get(key, 'NOT FOUND — inspect renamed test')
        reconciliation.append({'original_kind':kind, 'id':original, 'final_id':key, 'final_status':status})
        renamed_note = f' (now `{key.rsplit(".", 1)[-1]}`)' if key != original.split(' (')[0] else ''
        text.append(f'| `{original}` | {status}{renamed_note} |')
assert all(r['final_status']=='passed' for r in reconciliation)
(here/'fix-failure-reconciliation.json').write_text(json.dumps(reconciliation,indent=2)+'\n',encoding='utf-8')
report=back/'docs/test_fix_2026_10_18.md'
content=report.read_text(encoding='utf-8')
content=content.split('## Exact final results')[0]+'## Exact final results\n\n'+'\n'.join(text)+'\n'
report.write_text(content,encoding='utf-8')
print(json.dumps({'backend':dict(counts),'frontend':front['counts'],
                  'original_failure_events':len(reconciliation),
                  'unreconciled':[r for r in reconciliation if r['final_status']!='passed']}))
