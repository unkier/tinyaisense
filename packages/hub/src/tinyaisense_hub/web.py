"""The hub's HTTP port: uvicorn and Starlette, serving inside the hub's own loop.

`AgentWebSocketProtocol` subclasses uvicorn internals, so uvicorn's minor version is pinned
and `tests/test_web.py` guards what the subclass relies on.
"""

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import Generator
from typing import Any, cast

import uvicorn
from starlette.types import ASGIApp
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol

from .config import Timing
from .keepalive import Dead, Inbound, Keepalive, SendPing, Tick

MAX_MESSAGE = 1024 * 1024
"""The largest WebSocket message an agent may send."""
WRITE_HIGH = 4096
WRITE_LOW = 1024
"""Transport write-buffer limits: the writer waits once 4 KB is queued in the transport, so
that the hub's own lanes, where JSON goes before audio, hold the backlog instead."""

_log = logging.getLogger("tinyaisense_hub.web")


class AgentWebSocketProtocol(WebSocketsSansIOProtocol):
    """uvicorn's sans-IO WebSocket protocol, tuned for agents (ADR 0005).

    - Small transport write limits and TCP_NODELAY.
    - Reads eagerly: uvicorn pauses reading until the app takes each message, which would
      push back on an agent streaming speaker audio.
    - Keepalive by the hub's own `Keepalive`: a ping every `ws_ping_interval` s, and the link
      is dead after `ws_ping_timeout` s with no inbound frame.

    Compression is off in the server config.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # uvicorn pauses reading only while this is False. It sets it back to False only once
        # the app closes, and from then on discards messages before it would pause.
        self.read_paused = True
        self._keepalive: Keepalive | None = None
        self._keepalive_timer: asyncio.TimerHandle | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        self.transport.set_write_buffer_limits(high=WRITE_HIGH, low=WRITE_LOW)
        sock = cast(socket.socket | None, transport.get_extra_info("socket"))
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def data_received(self, data: bytes) -> None:
        if self._keepalive is not None:
            self._keepalive.handle(Inbound(), _now_us())
        super().data_received(data)

    async def receive(self) -> Any:
        return await self.queue.get()

    def start_keepalive(self) -> None:
        """Called once the handshake completes."""
        assert self.ping_interval is not None
        assert self.ping_timeout is not None
        self._keepalive = Keepalive(
            _now_us(),
            ping_interval=round(self.ping_interval * 1_000_000),
            dead_after=round(self.ping_timeout * 1_000_000),
        )
        self._run_keepalive()

    def stop_keepalive(self) -> None:
        super().stop_keepalive()
        self._keepalive = None
        if self._keepalive_timer is not None:
            self._keepalive_timer.cancel()
            self._keepalive_timer = None

    def _run_keepalive(self) -> None:
        self._keepalive_timer = None
        if self._keepalive is None:
            return
        now = _now_us()
        for action in self._keepalive.handle(Tick(), now):
            match action:
                case SendPing():
                    if not self.close_sent and not self.transport.is_closing():
                        self.conn.send_ping(b"")
                        self.transport.write(b"".join(self.conn.data_to_send()))
                case Dead():
                    self.keepalive_timeout()  # uvicorn's: close with 1011
        deadline = self._keepalive.next_deadline()
        if deadline is not None:
            self._keepalive_timer = self.loop.call_later(
                max(0, deadline - now) / 1_000_000, self._run_keepalive
            )


class HttpServer:
    """uvicorn serving `app` on a socket of our own, inside the running loop."""

    def __init__(self, app: ASGIApp, *, host: str | None, port: int, timing: Timing) -> None:
        self._app = app
        self._host = host
        self._port = port
        self._timing = timing
        self._server: _Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._socket: socket.socket | None = None

    @property
    def address(self) -> tuple[str, int]:
        assert self._socket is not None, "the server isn't running"
        host, port = cast(tuple[str, int], self._socket.getsockname()[:2])
        return host, port

    async def start(self) -> None:
        """Raises `OSError` if the port is taken."""
        # Binding here, not in uvicorn, makes a taken port an OSError instead of an exit.
        if self._host is None and socket.has_dualstack_ipv6():
            self._socket = socket.create_server(
                ("", self._port), family=socket.AF_INET6, dualstack_ipv6=True
            )
        else:
            self._socket = socket.create_server((self._host or "", self._port))
        config = uvicorn.Config(
            self._app,
            ws=AgentWebSocketProtocol,
            ws_max_size=MAX_MESSAGE,
            ws_per_message_deflate=False,
            ws_ping_interval=self._timing.agent_ping_interval_ms / 1000,
            ws_ping_timeout=self._timing.agent_dead_after_ms / 1000,
            lifespan="off",
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=1,
        )
        self._server = _Server(config)
        self._task = asyncio.create_task(self._server.serve([self._socket]))
        # uvicorn has no startup event to wait on.
        while not self._server.started:
            if self._task.done():
                self._task.result()
                raise RuntimeError("uvicorn exited while starting")
            await asyncio.sleep(0.001)

    async def close(self) -> None:
        server, task, sock = self._server, self._task, self._socket
        self._server = self._task = self._socket = None
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await task
            except Exception:
                _log.exception("the HTTP server failed")
        if sock is not None:
            sock.close()


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        # The hub owns the process's signals.
        yield


def _now_us() -> int:
    return time.monotonic_ns() // 1000
