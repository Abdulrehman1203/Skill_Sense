"""Review/apply the guarded Retell protocol; never expose credentials or call data."""
import json
from pathlib import Path
from urllib.parse import urlparse
import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


from interview_system.conversation_protocol import PROMPT, guarded_tool


class Command(BaseCommand):
    help = 'Inspect or install the server-controlled interview prompt/tool and conservative audio settings.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--callback-url')

    def handle(self, *args, **options):
        headers = {'Authorization': 'Bearer ' + settings.RETELL_API_KEY}
        def api(method, path, **kwargs):
            try:
                response = requests.request(method, 'https://api.retellai.com/' + path,
                    headers=headers, timeout=(3, 20), allow_redirects=False, **kwargs)
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError):
                raise CommandError('Retell configuration request failed; no raw provider details logged.') from None
        agent = api('GET', 'get-agent/' + settings.RETELL_AGENT_ID)
        engine = agent.get('response_engine', {})
        if engine.get('type') != 'retell-llm':
            raise CommandError('Only the configured single-prompt Retell LLM agent is supported.')
        llm = api('GET', 'get-retell-llm/' + engine['llm_id'])
        if llm.get('states'):
            raise CommandError('Review state-specific tools before converting this multi-state agent.')
        url = options['callback_url'] or settings.RETELL_CONVERSATION_CONTROL_URL
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise CommandError('Supply the public HTTPS interview-control callback URL.')
        if not parsed.path.endswith('/api/webhooks/retell/interview-control/'):
            raise CommandError('The callback must use /api/webhooks/retell/interview-control/.')
        tools = [t for t in llm.get('general_tools', []) if t.get('type') != 'end_call' and t.get('name') != 'interview_control']
        tools.append(guarded_tool(url))
        if not options['apply']:
            self.stdout.write(json.dumps({'agent': agent['agent_id'], 'callback_url': url,
                'unrestricted_end_tools_to_remove': sum(t.get('type') == 'end_call' for t in llm.get('general_tools', [])),
                'apply_required': True}))
            return
        # Backup operator-recoverable provider configuration, never tracked by Git.
        folder = Path(settings.BASE_DIR) / 'backups'
        folder.mkdir(exist_ok=True)
        backup = folder / ('retell-guard-' + timezone.now().strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        backup.write_text(json.dumps({'agent': agent, 'llm': llm}, ensure_ascii=False), encoding='utf-8')
        api('PATCH', 'update-retell-llm/' + engine['llm_id'], json={
            'general_prompt': PROMPT, 'general_tools': tools, 'tool_call_strict_mode': True})
        # Noise cancellation without aggressively suppressing a quiet candidate.
        api('PATCH', 'update-agent/' + settings.RETELL_AGENT_ID, json={
            'interruption_sensitivity': 0.8, 'denoising_mode': 'noise-cancellation',
            'end_call_after_silence_ms': 300000,
            'webhook_url': url.rsplit('/', 2)[0] + '/call-ended/'})
        checked = api('GET', 'get-retell-llm/' + engine['llm_id'])
        if any(t.get('type') == 'end_call' for t in checked.get('general_tools', [])):
            raise CommandError('An unrestricted ending tool remains; do not enable guarded calls.')
        if checked.get('general_prompt') != PROMPT or not any(t.get('name') == 'interview_control' and t.get('url') == url for t in checked.get('general_tools', [])):
            raise CommandError('Provider readback did not match the guarded configuration.')
        self.stdout.write(json.dumps({'configured': True, 'backup': str(backup), 'live_audio_verified': False}))
