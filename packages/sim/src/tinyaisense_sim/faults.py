"""Faults a simulated device can suffer. Each is live: set or trigger it at any time."""

import asyncio
import socket
import struct
from typing import cast

from tinyaisense_wire import Message, encode_control


class ConnectionSocket:
    """One Connection's TCP socket, as the device and its faults see it."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.vanished = False
        """Nothing goes out or comes in any more, and the device never closes it."""

    @property
    def raw(self) -> socket.socket:
        return cast(socket.socket, self.writer.get_extra_info("socket"))

    def send(self, message: Message) -> None:
        if not self.vanished:
            self.writer.write(encode_control(message))

    def reset(self) -> None:
        """Close with an RST."""
        self.raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self.writer.transport.abort()

    def close(self) -> None:
        """Close with a FIN, after what was sent."""
        self.writer.close()

    def pause_reading(self) -> None:
        cast(asyncio.Transport, self.writer.transport).pause_reading()

    def resume_reading(self) -> None:
        transport = cast(asyncio.Transport, self.writer.transport)
        if not transport.is_closing():
            transport.resume_reading()


class Faults:
    """A device's faults on its TCP Connection. They act on the Connection of the moment."""

    def __init__(self) -> None:
        self._connection: ConnectionSocket | None = None
        self._stall_reads = False
        self._reads_resumed = asyncio.Event()
        self._reads_resumed.set()

    @property
    def connection(self) -> ConnectionSocket | None:
        """Set by the device while it has a Connection."""
        return self._connection

    @connection.setter
    def connection(self, connection: ConnectionSocket | None) -> None:
        self._connection = connection
        if connection is not None and self._stall_reads:
            connection.pause_reading()

    def rst(self) -> None:
        """Reset the Connection: the hub gets an RST."""
        if self._connection is not None and not self._connection.vanished:
            self._connection.reset()

    def close(self) -> None:
        """Close the Connection cleanly: the hub gets a FIN."""
        if self._connection is not None and not self._connection.vanished:
            self._connection.close()

    def vanish(self) -> None:
        """Go silent on the Connection both ways, like a device whose WiFi died.

        The device gives up on it after its own dead timeout, then reconnects. The hub is
        never told: it has to notice the silence.
        """
        if self._connection is not None:
            self._connection.vanished = True

    @property
    def stall_reads(self) -> bool:
        """While set, the device reads nothing from the hub. What the hub sends backs up
        into TCP, and what had already come in waits."""
        return self._stall_reads

    @stall_reads.setter
    def stall_reads(self, stall: bool) -> None:
        self._stall_reads = stall
        if stall:
            self._reads_resumed.clear()
            if self._connection is not None:
                self._connection.pause_reading()
        else:
            self._reads_resumed.set()
            if self._connection is not None:
                self._connection.resume_reading()

    async def readable(self, connection: ConnectionSocket) -> None:
        """Return once the device may take in what it read: never, if it vanished."""
        while self._stall_reads or connection.vanished:
            if connection.vanished:
                await asyncio.Event().wait()
            await self._reads_resumed.wait()
