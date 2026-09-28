"""Guards what `web.AgentWebSocketProtocol` relies on in uvicorn's internals (ADR 0005)."""

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest
import websockets
from starlette.types import Receive, Scope, Send
from tinyaisense_hub import Timing
from tinyaisense_hub.web import AgentWebSocketProtocol, HttpServer


class LazyApp:
    """Accepts each WebSocket, then reads nothing until `read` is set."""

    def __init__(self) -> None:
        self.read = asyncio.Event()
        self.messages = 0
        self.disconnected = asyncio.Event()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["type"] == "websocket"
        await receive()  # websocket.connect
        await send({"type": "websocket.accept"})
        await self.read.wait()
        while (await receive())["type"] == "websocket.receive":
            self.messages += 1
        self.disconnected.set()


@pytest.fixture
def app() -> LazyApp:
    return LazyApp()


@pytest.fixture
async def server(app: LazyApp) -> AsyncIterator[HttpServer]:
    server = HttpServer(app, host="127.0.0.1", port=0, timing=Timing())
    await server.start()
    yield server
    app.read.set()
    await server.close()


def url(server: HttpServer) -> str:
    host, port = server.address
    return f"ws://{host}:{port}/agent"


def the_link(server: HttpServer) -> AgentWebSocketProtocol:
    uvicorn_server = server._server  # pyright: ignore[reportPrivateUsage]
    assert uvicorn_server is not None
    [link] = uvicorn_server.server_state.connections
    assert isinstance(link, AgentWebSocketProtocol)
    return link


async def test_a_link_has_small_write_limits_nodelay_and_no_deflate(server: HttpServer) -> None:
    async with websockets.connect(url(server), compression="deflate") as ws:
        link = the_link(server)
        sock: socket.socket = link.transport.get_extra_info("socket")

        assert link.transport.get_write_buffer_limits() == (1024, 4096)
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        assert ws.response is not None
        assert "Sec-WebSocket-Extensions" not in ws.response.headers


async def test_a_link_is_read_before_the_app_asks(server: HttpServer, app: LazyApp) -> None:
    message = bytes(64 * 1024)
    count = 512  # 32 MB: more than the kernel buffers hold, if the hub stopped reading

    async with websockets.connect(url(server), ping_interval=None) as ws:
        async with asyncio.timeout(10):
            for _ in range(count):
                await ws.send(message)
        app.read.set()

    await asyncio.wait_for(app.disconnected.wait(), 5)
    assert app.messages == count
