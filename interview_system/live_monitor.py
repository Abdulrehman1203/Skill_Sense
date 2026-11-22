"""Session-scoped, ephemeral signaling. Media never enters Channels."""
import asyncio
import base64
from contextlib import suppress
import hashlib
import hmac
import json
from ai_interview_recruitment.safe_logging import emit_event
import time
import uuid

from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings

from .consumers import io
from .socket_auth import SocketAuthorizationError, check_lease_async, redeem_ticket_async
from .socket_guard import SocketGuard

INTERVAL = 2
MAX_BYTES = 32768


def ice_configuration():
    secret = settings.LIVE_MONITOR_TURN_SECRET
    urls = settings.LIVE_MONITOR_TURN_URLS
    if not secret or not urls or any(not u.startswith(('turn:', 'turns:')) for u in urls):
        raise ValueError('relay_unavailable')
    username = f'{int(time.time()) + 600}:{uuid.uuid4().hex}'
    credential = base64.b64encode(hmac.digest(secret.encode(), username.encode(), 'sha1')).decode()
    servers = [{'urls': urls, 'username': username, 'credential': credential}]
    if settings.LIVE_MONITOR_STUN_URLS:
        servers.append({'urls': settings.LIVE_MONITOR_STUN_URLS})
    return {'iceServers': servers, 'iceTransportPolicy': 'relay' if settings.LIVE_MONITOR_RELAY_ONLY else 'all'}


def validate_description(message, role):
    expected = 'offer' if role == 'camera' else 'answer'
    direction = 'a=sendonly' if role == 'camera' else 'a=recvonly'
    if set(message) != {'type', 'generation', 'sdp'} or message['type'] != expected:
        raise ValueError('invalid_signal')
    sections = message['sdp'].replace('\r\n', '\n').split('\nm=')
    if len(sections) != 3 or {s.split(' ', 1)[0] for s in sections[1:]} != {'audio', 'video'}:
        raise ValueError('invalid_media')
    for section in sections[1:]:
        directions = [line for line in section.splitlines() if line in
                      ('a=sendonly', 'a=recvonly', 'a=sendrecv', 'a=inactive')]
        if directions != [direction]:
            raise ValueError('invalid_direction')


class LiveMonitorSocket(AsyncWebsocketConsumer):
    guard = timer = peer = generation = None
    closed = sent_description = accepted = False

    async def connect(self):
        self.lock = asyncio.Lock()
        params = self.scope['url_route']['kwargs']
        self.role = params['role']
        try:
            if self.role not in ('camera', 'viewer'):
                raise ValueError('invalid_role')
            self.lease = await io(redeem_ticket_async(self.scope.pop('interview_analysis_ticket', None),
                                                     params['session_id'], self.role))
            if self.role == 'viewer':
                emit_event('monitor_attempt')
            self.guard = SocketGuard(self.lease, self.role, self.channel_name)
            self.mode_key = self.guard.prefix + ':mode'
            other = 'viewer' if self.role == 'camera' else 'camera'
            self.other_key = f'{self.guard.prefix}:connections:session:{self.lease.session_id}:{other}'
            config = ice_configuration()
            mode = await io(self.guard.redis.get(self.mode_key))
            if mode == b'off' or (self.role == 'viewer' and mode == b'drain'):
                raise SocketAuthorizationError('monitor_unavailable')
            if not await io(self.guard.reserve()):
                raise SocketAuthorizationError('connection_limit', 409)
            await self.accept()
            self.accepted = True
            if self.role == 'viewer':
                emit_event('monitor_join')
            await self.emit({'type': 'ready', 'config': config})
            await self.refresh()
            self.timer = asyncio.create_task(self.watch())
        except Exception as error:
            await self.fail(error)

    async def emit(self, message):
        await io(self.send(text_data=json.dumps(message, separators=(',', ':'))))

    async def refresh(self):
        await io(check_lease_async(self.lease))
        if not await io(self.guard.reserve(refresh=True)) or await io(self.guard.redis.get(self.mode_key)) == b'off':
            raise SocketAuthorizationError('monitor_unavailable')
        seconds, micros = await io(self.guard.redis.time())
        peers = await io(self.guard.redis.zrangebyscore(self.other_key, seconds * 1000 + micros / 1000, '+inf'))
        peer = peers[0].decode() if peers else None
        if peer != self.peer:
            self.peer = peer
            self.sent_description = self.measured = False
            members = sorted((self.channel_name, peer)) if peer else []
            self.generation = hashlib.sha256('|'.join(members).encode()).hexdigest() if peer else None
        await self.emit({'type': 'presence', 'generation': self.generation})

    async def watch(self):
        try:
            while True:
                await asyncio.sleep(INTERVAL)
                async with self.lock:
                    if self.closed:
                        return
                    await self.refresh()
        except Exception as error:
            await self.fail(error)

    async def receive(self, text_data=None, bytes_data=None):
        async with self.lock:
            if self.closed:
                return
            try:
                if text_data is None or len(text_data.encode()) > MAX_BYTES or not await io(self.guard.allow('signal')):
                    raise ValueError('invalid_signal')
                message = json.loads(text_data)
                await self.refresh()
                if not self.peer or message.get('generation') != self.generation:
                    raise ValueError('stale_signal')
                if self.role == 'viewer' and message.get('type') == 'metric':
                    ms, relay = message.get('milliseconds'), message.get('relay')
                    if not self.sent_description or self.measured or type(ms) is not int or not 0 <= ms <= 86400000 or type(relay) is not bool:
                        raise ValueError('invalid_metric')
                    self.measured = True
                    emit_event('monitor_media', duration_ms=ms)
                    if relay:
                        emit_event('monitor_relay')
                    if message.get('reconnect') is True:
                        emit_event('monitor_reconnect')
                    return
                if self.sent_description:
                    raise ValueError('stale_signal')
                validate_description(message, self.role)
                self.sent_description = True
                await io(self.channel_layer.send(self.peer, {'type': 'monitor.signal', 'message': message}))
            except Exception as error:
                await self.fail(error)

    async def monitor_signal(self, event):
        async with self.lock:
            if self.closed:
                return
            try:
                await self.refresh()
                if self.generation and self.generation == event['message']['generation']:
                    await self.emit(event['message'])
            except Exception as error:
                await self.fail(error)

    async def fail(self, error):
        self.closed = True
        reason = error.reason if isinstance(error, SocketAuthorizationError) else 'unavailable'
        if self.guard and not self.accepted and reason in ('monitor_unavailable', 'connection_limit'):
            # Browsers hide pre-upgrade denial codes. Only a redeemed owner ticket reaches this path.
            await self.accept()
            self.accepted = True
        await self.close(code={'monitor_unavailable': 4403, 'session_not_publishable': 4410, 'access_denied': 4403,
                               'connection_limit': 4409, 'lease_expired': 4401}.get(reason, 4500))

    async def disconnect(self, code):
        self.closed = True
        if self.role == 'viewer' and self.guard:
            emit_event('monitor_closed')
        if self.timer:
            self.timer.cancel()
            await asyncio.gather(self.timer, return_exceptions=True)
        if self.guard:
            with suppress(Exception):
                await io(self.guard.release())
            await self.guard.aclose()
