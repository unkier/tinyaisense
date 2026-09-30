"""A hub's TCP side that does only what the test tells it to, for Simulator tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

from tinyaisense_sim import SimDevice
from tinyaisense_wire import (
    Command,
    ControlDecoder,
    Event,
    EventMirror,
    FailedReply,
    Hello,
    JsonObject,
    Message,
    Ping,
    Pong,
    Reply,
    StreamFrame,
    decode_datagram,
    encode_control,
    encode_datagram,
)

CONN_ID = 7
"""The `conn_id` the raw hub welcomes a device with."""


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

    async def event(self) -> Event:
        message = await self.receive()
        assert isinstance(message, Event), message
        return message

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


class RawUdp(asyncio.DatagramProtocol):
    """The hub's UDP socket: every datagram that comes in, in order."""

    def __init__(self) -> None:
        self._datagrams: asyncio.Queue[bytes] = asyncio.Queue()
        self._transport: asyncio.DatagramTransport | None = None

    @classmethod
    async def open(cls) -> RawUdp:
        _, udp = await asyncio.get_running_loop().create_datagram_endpoint(
            cls, local_addr=("127.0.0.1", 0)
        )
        return udp

    @property
    def port(self) -> int:
        assert self._transport is not None
        return cast(tuple[str, int], self._transport.get_extra_info("sockname"))[1]

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._datagrams.put_nowait(data)

    async def frame(self) -> StreamFrame:
        """The next stream frame, past any event mirrors."""
        while not isinstance(datagram := decode_datagram(await self._datagrams.get()), StreamFrame):
            assert isinstance(datagram, EventMirror)
        return datagram

    async def mirror(self) -> EventMirror:
        datagram = decode_datagram(await self._datagrams.get())
        assert isinstance(datagram, EventMirror)
        return datagram

    def send(self, frame: StreamFrame, port: int) -> None:
        """Send a stream frame to the device's UDP `port`, on this machine."""
        assert self._transport is not None
        self._transport.sendto(encode_datagram(frame), ("127.0.0.1", port))

    async def frames(self, count: int) -> list[StreamFrame]:
        return [await self.frame() for _ in range(count)]

    def pending(self) -> list[StreamFrame]:
        """The frames in so far, past any event mirrors, without waiting."""
        datagrams = [
            decode_datagram(self._datagrams.get_nowait()) for _ in range(self._datagrams.qsize())
        ]
        return [d for d in datagrams if isinstance(d, StreamFrame)]

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()


@dataclass
class Online:
    """A simulated device the raw hub has welcomed, and the raw hub's UDP socket."""

    connection: RawConnection
    udp: RawUdp
    device: SimDevice
    next_id: int = 1

    async def command(
        self, cap: str | None, name: str, args: JsonObject | None = None
    ) -> Reply | FailedReply:
        command_id, self.next_id = self.next_id, self.next_id + 1
        self.connection.send(Command(command_id, cap, name, args or {}))
        while True:
            message = await self.connection.receive()
            if isinstance(message, Reply | FailedReply) and message.id == command_id:
                return message

    async def ok(self, cap: str | None, name: str, args: JsonObject | None = None) -> JsonObject:
        reply = await self.command(cap, name, args)
        assert isinstance(reply, Reply), reply
        return reply.result

    async def clock(self) -> int:
        """The device clock now, from a pong."""
        self.connection.send(Ping(0))
        while not isinstance(message := await self.connection.receive(), Pong):
            pass
        return message.ts
