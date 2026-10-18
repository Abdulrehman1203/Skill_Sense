"""Local, fixture-only browser auth smoke; never attach to a user's profile."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading

HERE = Path(__file__).resolve().parent
OUTPUT = Path('D:/Programming/FYP/Skill_Sense_frontend/build/auth_contract_smoke')
assert OUTPUT.resolve().is_relative_to(Path('D:/Programming/FYP/Skill_Sense_frontend/build').resolve())
assert (OUTPUT/'main.dart.js').is_file()
# Only the isolated compiled TEST output is changed. Production web/index.html
# and the actual Clerk loader remain untouched. Real provider networking is
# blocked by CSP, and this target supplies its own fake Clerk object.
(OUTPUT/'index.html').write_text('''<!doctype html><meta charset="utf-8">
<title>TEST ONLY local authentication contract</title>
<script>window.PasskeyAuthenticator={init(){}};</script>
<script src="flutter_bootstrap.js" async></script>''', encoding='utf-8')

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args): pass
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self' blob: data:; script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval'; connect-src 'self' blob: data:; style-src 'self' 'unsafe-inline'")
        super().end_headers()

server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(OUTPUT)))
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
report = {'fixture_only': True, 'real_clerk_provider': False, 'camera_or_microphone': False,
          'test_target': 'test/web_auth_lifecycle_check.dart', 'checks': 2}
try:
    with tempfile.TemporaryDirectory(prefix='skillsense-auth-contract-') as profile:
        assert Path(profile).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
        result = subprocess.run(['C:/Program Files/Google/Chrome/Application/chrome.exe',
            '--headless=new', '--no-first-run', '--no-default-browser-check',
            '--disable-background-networking', '--disable-component-update',
            '--user-data-dir='+profile, '--virtual-time-budget=15000', '--dump-dom',
            f'http://127.0.0.1:{server.server_port}/'], capture_output=True, timeout=45,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        dom = result.stdout.decode('utf-8', errors='replace')
        report['passed'] = 'PASS: 2 web authentication checks' in dom
        report['browser_exit_code'] = result.returncode
        if not report['passed']:
            (HERE/'fix-browser-auth-dom.html').write_text(dom, encoding='utf-8')
except subprocess.TimeoutExpired:
    report.update(passed=False, reason='owned browser smoke exceeded 45-second bound')
finally:
    server.shutdown()
    server.server_close()
(HERE/'fix-browser-auth-smoke.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
print(json.dumps(report))
raise SystemExit(0 if report.get('passed') else 1)
