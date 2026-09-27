"""The device side of a Connection, driven by a hand-rolled hub that can misbehave."""

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import pytest
from tinyaisense_sim.pytest_plugin import SimulatorFactory
from tinyaisense_sim.simulator import DEAD_AFTER_MS
from tinyaisense_wire import ControlDecoder, Hello, Message, Pending, Ping, Pong, encode_control

ECHO = "m5stack-atom-echo"


class RawConnection:
    """The hub's end of one device Connection."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._decoder = ControlDecoder()
        self._inbox: list[Message] = []

    def send(self, message: Message) -> None:
        self._writer.write(encode_control(message))

    async def receive(self) -> Message:
        while not self._inbox:
            data = await self._reader.read(65536)
            assert data, "the device closed the Connection"
            self._inbox.extend(self._decoder.feed(data))
        return self._inbox.pop(0)

    async def until_closed(self) -> list[Message]:
        """Everything the device sends until it closes the Connection."""
        while data := await self._reader.read(65536):
            self._inbox.extend(self._decoder.feed(data))
        messages, self._inbox = self._inbox, []
        return messages

    def close(self) -> None:
        self._writer.close()


class RawHub:
    """A hub's TCP side that does only what the test tells it to."""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._connections: asyncio.Queue[RawConnection] = asyncio.Queue()
        self._all: list[RawConnection] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._connected, "127.0.0.1", 0)

    @property
    def address(self) -> tuple[str, int]:
        assert self._server is not None
        return cast(tuple[str, int], self._server.sockets[0].getsockname()[:2])

    async def accept(self) -> RawConnection:
        """The next device Connection, once its `hello` is in."""
        connection = await self._connections.get()
        assert isinstance(await connection.receive(), Hello)
        return connection

    async def close(self) -> None:
        for connection in self._all:
            connection.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def _connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = RawConnection(reader, writer)
        self._all.append(connection)
        self._connections.put_nowait(connection)


@pytest.fixture
async def raw_hub() -> AsyncIterator[RawHub]:
    hub = RawHub()
    await hub.start()
    yield hub
    await hub.close()


async def pending_device(
    raw_hub: RawHub, simulator: SimulatorFactory, dead_after_ms: int = DEAD_AFTER_MS
) -> RawConnection:
    """Start a simulated Echo against `raw_hub` and leave it pending."""
    sim = simulator(raw_hub.address, dead_after_ms=dead_after_ms)
    added = asyncio.create_task(sim.add(ECHO, "kitchen"))
    connection = await raw_hub.accept()
    connection.send(Pending())
    await added
    return connection


async def test_a_device_answers_a_ping_with_its_own_clock(
    raw_hub: RawHub, simulator: SimulatorFactory
) -> None:
    connection = await pending_device(raw_hub, simulator)

    connection.send(Ping(123))
    first = await connection.receive()
    connection.send(Ping(456))
    second = await connection.receive()

    assert isinstance(first, Pong)
    assert isinstance(second, Pong)
    assert (first.t, second.t) == (123, 456)
    assert second.ts > first.ts


async def test_a_device_drops_a_silent_hub_and_reconnects(
    raw_hub: RawHub, simulator: SimulatorFactory
) -> None:
    connection = await pending_device(raw_hub, simulator, dead_after_ms=300)

    assert await connection.until_closed() == []
    await raw_hub.accept()


async def test_pings_from_the_hub_keep_the_connection_alive(
    raw_hub: RawHub, simulator: SimulatorFactory
) -> None:
    connection = await pending_device(raw_hub, simulator, dead_after_ms=400)

    # Twenty pings, 50 ms apart, outlast the dead timeout more than twice over.
    for t in range(20):
        connection.send(Ping(t))
        pong = await connection.receive()
        assert isinstance(pong, Pong)
        assert pong.t == t
        await asyncio.sleep(0.05)
