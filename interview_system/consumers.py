"""Interview Analysis transport only: private bounded validation, never model inference."""
import asyncio
from contextlib import suppress
import json
import uuid

from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from . import frame_admission, observer_delivery, socket_db
from .frame_validation import ProtocolError, get_decoder, parse_message
from .socket_auth import SocketAuthorizationError
from .socket_guard import SocketGuard


def observer_group(session_id):
    return f'analysis.observe.{session_id}'


async def io(awaitable):
    return await asyncio.wait_for(awaitable, max(0.05, settings.INTERVIEW_ANALYSIS_IO_TIMEOUT_SECONDS))


def auth_close(reason):
    if reason in ('lease_expired', 'credential_expired', 'ticket_invalid'):
        return 4401
    if reason in ('publisher_conflict', 'connection_limit', 'session_not_publishable', 'stale_connection'):
        return 4409
    return 4403


class AnalysisSocket(AsyncWebsocketConsumer):
    purpose = None
    groups = []  # Never auto-join any group before authentication.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accepted = False
        self.closed = False
        self.cleaned = False
        self.lease = None
        self.guard = None
        self.generation = None
        self.group_name = None
        self.watchdog = None
        self.acquisition = None
        self.lock = asyncio.Lock()
        self.last_sequence = -1
        self.last_revision = -1

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        except Exception:
            # Do not log scopes, ticket values, raw payloads or exception locals.
            with suppress(Exception):
                await self.terminate('authorization_unavailable', 1011)
        finally:
            await self.cleanup()

    @sensitive_variables()
    async def connect(self):
        try:
            secret = self.scope.pop('interview_analysis_ticket', None)
            session_id = self.scope['url_route']['kwargs']['session_id']
            self.lease = await io(socket_db.redeem(secret, session_id, self.purpose))
            secret = None
            self.guard = SocketGuard(self.lease, self.purpose, uuid.uuid4().hex)
            if not await io(self.guard.reserve()):
                raise SocketAuthorizationError('connection_limit', 409)
            if self.purpose == 'publisher':
                # If an I/O timeout cancels the waiter, retain the acquisition
                # result long enough for fenced cleanup (sync SQL is not cancellable).
                self.acquisition = asyncio.create_task(socket_db.acquire(self.lease))
                self.generation = await io(asyncio.shield(self.acquisition))
            else:
                self.group_name = observer_group(self.lease.session_id)
                await io(self.channel_layer.group_add(self.group_name, self.channel_name))
            await self.authorize()
            await self.accept()
            self.accepted = True
            ready = {'type': 'ready', 'protocol_version': 1,
                     'lease_expires_at': self.lease.expires_at.isoformat().replace('+00:00', 'Z'),
                     'auth_check_seconds': self.watch_interval()}
            if self.purpose == 'publisher':
                ready.update(connection_generation=str(self.generation), target_fps=min(3, settings.INTERVIEW_ANALYSIS_FRAME_FPS),
                    max_message_bytes=settings.INTERVIEW_ANALYSIS_MAX_MESSAGE_BYTES, max_jpeg_bytes=settings.INTERVIEW_ANALYSIS_MAX_JPEG_BYTES,
                    max_pixels=settings.INTERVIEW_ANALYSIS_MAX_PIXELS, max_width=settings.INTERVIEW_ANALYSIS_MAX_WIDTH, max_height=settings.INTERVIEW_ANALYSIS_MAX_HEIGHT)
            else:
                ready['session_id'] = str(self.lease.session_id)
            await self.emit(ready)
            if self.purpose == 'observer':
                await self.snapshot()  # Subscribe first, then apply highest revision.
            self.watchdog = asyncio.create_task(self.watch())
        except ProtocolError as exc:
            await self.terminate(exc.reason, exc.code)
        except SocketAuthorizationError as exc:
            await self.terminate(exc.reason, auth_close(exc.reason))
        except Exception:
            await self.terminate('authorization_unavailable', 1011)

    async def authorize(self):
        if self.closed or self.lease is None:
            raise SocketAuthorizationError('access_denied')
        await io(socket_db.check(self.lease, self.generation))
        if self.guard is not None and not await io(self.guard.reserve(refresh=True)):
            raise SocketAuthorizationError('access_denied')

    def watch_interval(self):
        return min(max(0.05, settings.INTERVIEW_ANALYSIS_AUTH_CHECK_SECONDS),
                   max(0.05, settings.INTERVIEW_ANALYSIS_GUARD_LEASE_SECONDS / 3),
                   max(0.05, settings.INTERVIEW_ANALYSIS_CONNECTION_LEASE_SECONDS / 3))

    async def emit(self, message):
        encoded = json.dumps(message, allow_nan=False, separators=(',', ':'))
        if len(encoded.encode('utf-8')) > 65536:
            raise ValueError('Server message exceeds bounded envelope.')
        await io(self.send(text_data=encoded))

    @sensitive_variables()
    async def receive(self, text_data=None, bytes_data=None):
        received_at = timezone.now()
        async with self.lock:
            if self.closed:
                return
            try:
                if bytes_data is not None:
                    raise ProtocolError('invalid_message')
                if text_data is None:
                    raise ProtocolError('invalid_message')
                try:
                    size = len(text_data.encode('utf-8'))
                except UnicodeEncodeError:
                    raise ProtocolError('invalid_message') from None
                if size > settings.INTERVIEW_ANALYSIS_MAX_MESSAGE_BYTES:
                    raise ProtocolError('message_too_large', 1009)
                await self.authorize()
                if not await io(self.guard.allow('message')):
                    raise ProtocolError('invalid_message')
                message = parse_message(text_data)
                if message['type'] == 'ping':
                    if self.purpose == 'publisher':
                        await io(socket_db.heartbeat(self.lease, self.generation))
                    await self.authorize()
                    await self.emit({'type': 'pong', 'server_ts': timezone.now().isoformat().replace('+00:00', 'Z')})
                elif self.purpose == 'publisher':
                    await self.frame(message, received_at)
                else:
                    raise ProtocolError('invalid_message')
            except ProtocolError as exc:
                await self.terminate(exc.reason, exc.code)
            except SocketAuthorizationError as exc:
                await self.terminate(exc.reason, auth_close(exc.reason))
            except Exception:
                await self.terminate('authorization_unavailable', 1011)

    @sensitive_variables()
    async def frame(self, message, received_at):
        sequence = message['sequence']
        if sequence <= self.last_sequence:
            raise ProtocolError('invalid_sequence')
        self.last_sequence = sequence
        if not await io(self.guard.allow('frame')):
            await self.ack(sequence, 'dropped', 'rate_limited')
            return
        jpeg = await get_decoder().decode(message['data'])
        if jpeg is None:
            await self.ack(sequence, 'dropped', 'capacity')
            return
        await self.authorize()  # Decode may have outlived credentials/account eligibility.
        decision = await io(frame_admission.submit_frame(lease=self.lease, generation=self.generation,
            sequence=sequence, capture_ts=message['capture_ts'], received_at=received_at, jpeg=jpeg))
        if (not isinstance(decision, frame_admission.AdmissionDecision)
                or decision.status not in ('accepted', 'dropped')
                or (decision.status == 'accepted' and decision.reason is not None)
                or (decision.status == 'dropped' and decision.reason not in ('capacity', 'rate_limited', 'stale'))):
            raise ValueError('Invalid admission decision.')
        await self.ack(sequence, decision.status, decision.reason)

    async def ack(self, sequence, status, reason=None):
        if status == 'dropped':
            await io(socket_db.drop(self.lease, self.generation, sequence))
        await self.authorize()
        message = {'type': 'ack', 'connection_generation': str(self.generation), 'sequence': sequence, 'status': status}
        if reason is not None:
            message['reason'] = reason
        await self.emit(message)

    async def snapshot(self):
        await self.authorize()
        snapshot = await io(observer_delivery.get_snapshot(self.lease))
        if snapshot is None:
            return
        try:
            observer_delivery.validate_snapshot(snapshot, self.lease.session_id)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ProtocolError('internal_error', 1011) from None
        if snapshot['revision'] <= self.last_revision:
            return
        await self.authorize()
        await self.emit(snapshot)
        self.last_revision = snapshot['revision']

    async def analysis_update(self, event):
        # Publishers never receive observations, even if a producer sends directly
        # to their private channel by mistake. Notification payloads are not forwarded.
        if self.purpose != 'observer':
            return
        async with self.lock:
            if self.closed:
                return
            try:
                await self.snapshot()
            except SocketAuthorizationError as exc:
                await self.terminate(exc.reason, auth_close(exc.reason))
            except Exception:
                await self.terminate('internal_error', 1011)

    async def watch(self):
        try:
            while not self.closed:
                remaining = (self.lease.expires_at - timezone.now()).total_seconds()
                await asyncio.sleep(max(0.01, min(self.watch_interval(), remaining)))
                async with self.lock:
                    if self.closed:
                        return
                    await self.authorize()
                    if self.group_name is not None:
                        await io(self.channel_layer.group_add(self.group_name, self.channel_name))
                        # At-most-once channel notifications can be lost. Persisted
                        # revision polling also converges an already-open observer.
                        await self.snapshot()
        except asyncio.CancelledError:
            raise
        except SocketAuthorizationError as exc:
            await self.terminate(exc.reason, auth_close(exc.reason))
        except Exception:
            await self.terminate('authorization_unavailable', 1011)

    async def terminate(self, reason, code):
        if self.closed:
            return
        self.closed = True  # Fence admission/delivery before any cleanup I/O.
        await self.cleanup()
        if self.accepted:
            with suppress(Exception):
                await self.emit({'type': 'error', 'reason': reason})
        with suppress(Exception):
            await io(self.close(code=code))

    async def disconnect(self, close_code):
        self.closed = True
        await self.cleanup()

    async def cleanup(self):
        if self.cleaned:
            return
        self.cleaned = True
        if self.watchdog is not None and self.watchdog is not asyncio.current_task():
            self.watchdog.cancel()
            await asyncio.gather(self.watchdog, return_exceptions=True)
        if self.group_name is not None:
            with suppress(Exception):
                await io(self.channel_layer.group_discard(self.group_name, self.channel_name))
        if self.generation is not None:
            with suppress(Exception):
                await io(socket_db.disconnect(self.lease.session_id, self.generation))
        elif self.acquisition is not None:
            # A timed-out sync acquisition can finish after this coroutine. Its
            # generation is cleaned specifically, never against a newer publisher.
            def late_cleanup(task):
                with suppress(BaseException):
                    generation = task.result()
                    async def compensate():
                        with suppress(Exception):
                            await io(socket_db.disconnect(self.lease.session_id, generation))
                    asyncio.create_task(compensate())
            self.acquisition.add_done_callback(late_cleanup)
        if self.guard is not None:
            with suppress(Exception):
                await io(self.guard.release())
            with suppress(Exception):
                await io(self.guard.aclose())


class PublisherConsumer(AnalysisSocket):
    purpose = 'publisher'


class ObserverConsumer(AnalysisSocket):
    purpose = 'observer'
