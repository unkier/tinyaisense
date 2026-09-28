"""A hand-rolled device that can break the protocol, for tests that need to."""

import asyncio
from typing import Self

from tinyaisense_hub import Hub
from tinyaisense_sim import PROFILES
from tinyaisense_wire import ControlDecoder, Manifest, Message, Ping, encode_control

DEVICE_ID = "02aabbccddee"


def echo_manifest(device_id: str = DEVICE_ID) -> Manifest:
    return PROFILES["m5stack-atom-echo"].manifest(device_id, "raw-1")


class RawDevice:
    """A device's TCP side, one message at a time. It never answers pings by itself."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._decoder = ControlDecoder()
        self._inbox: list[Message] = []

    @classmethod
    async def connect(cls, hub: Hub) -> Self:
        return cls(*await asyncio.open_connection(*hub.device_address))

    def send(self, message: Message) -> None:
        self._writer.write(encode_control(message))

    def send_bytes(self, data: bytes) -> None:
        self._writer.write(data)

    def close(self) -> None:
        self._writer.close()

    async def receive(self) -> Message:
        """The next message that isn't a ping."""
        while (message := self._pop(ping=False)) is None:
            assert await self._read(), "the hub closed the Connection"
        return message

    async def receive_ping(self) -> Ping:
        while (ping := self._pop(ping=True)) is None:
            assert await self._read(), "the hub closed the Connection"
        assert isinstance(ping, Ping)
        return ping

    async def until_closed(self) -> list[Message]:
        """Everything but pings that the hub sends until it closes the Connection."""
        while await self._read():
            pass
        messages: list[Message] = [m for m in self._inbox if not isinstance(m, Ping)]
        self._inbox = []
        return messages

    async def _read(self) -> bool:
        data = await self._reader.read(65536)
        self._inbox.extend(self._decoder.feed(data))
        return bool(data)

    def _pop(self, *, ping: bool) -> Message | None:
        for i, message in enumerate(self._inbox):
            if isinstance(message, Ping) == ping:
                return self._inbox.pop(i)
        return None
