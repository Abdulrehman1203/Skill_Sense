"""Pinned Daphne 4.2.2 entry point with a bounded WebSocket ASGI input queue.

Run: python -m ai_interview_recruitment.socket_server -b 127.0.0.1 -p 8000 ai_interview_recruitment.asgi:application
Uses Daphne internals verified for the pinned version; HTTP remains unchanged.
"""
import asyncio
from functools import partial

# Daphne installs its Twisted asyncio reactor first, as required by its API.
from daphne.server import Server
from daphne.cli import CommandLineInterface
from django.conf import settings


class BoundedSocketQueue(asyncio.Queue):
    def __init__(self, size, overflow):
        super().__init__(maxsize=max(1, size))
        self.overflow = overflow
        self.closed = False

    def _clear(self):
        while not self.empty():
            self.get_nowait()

    def put_nowait(self, item):
        if self.closed:
            return
        if item['type'] == 'websocket.disconnect':
            self.closed = True
            self._clear()
            return super().put_nowait(item)
        if self.full():
            self.closed = True
            self._clear()
            super().put_nowait({'type': 'websocket.disconnect', 'code': 1008})
            self.overflow()
            return
        return super().put_nowait(item)


class BoundedDaphneServer(Server):
    def __init__(self, *args, **kwargs):
        for name in ('websocket_max_message_size', 'websocket_max_frame_size'):
            limit = kwargs.setdefault(name, 307200)
            if not 1 <= limit <= 307200:
                raise ValueError('Interview Analysis wire limits must be between 1 and 307200 bytes.')
        super().__init__(*args, **kwargs)

    def create_application(self, protocol, scope):
        if scope['type'] != 'websocket':
            return super().create_application(protocol, scope)
        assert 'application_instance' not in self.connections[protocol]
        def overflow():
            protocol.handle_reply({'type': 'websocket.close', 'code': 1008})
            protocol.muted = True
        queue = BoundedSocketQueue(settings.INTERVIEW_ANALYSIS_ASGI_INPUT_MESSAGES, overflow)
        scope.setdefault('asgi', {'version': '3.0'})
        scope['interview_analysis_bounded_input'] = True  # Trusted server marker, never a client header.
        application = self.application(scope=scope, receive=queue.get, send=partial(self.handle_reply, protocol))
        if protocol not in self.connections:
            return None
        self.connections[protocol]['application_instance'] = asyncio.ensure_future(application, loop=asyncio.get_event_loop())
        return queue


class SocketCLI(CommandLineInterface):
    server_class = BoundedDaphneServer

    def __init__(self):
        super().__init__()
        self.parser.set_defaults(websocket_max_message_size=307200, websocket_max_frame_size=307200,
                                 websocket_timeout=60, websocket_connect_timeout=5, verbosity=0,
                                 application='ai_interview_recruitment.asgi:application')
        # The base CLI has a required positional application; retain the normal
        # Daphne CLI signature in the documented command rather than patch it.


if __name__ == '__main__':
    SocketCLI.entrypoint()
