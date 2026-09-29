"""Faults a simulated device can suffer. Each is live: set or trigger it at any time."""

import asyncio
import random
import socket
import struct
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import cast

from tinyaisense_wire import Message, StreamFrame, WireError, decode_datagram, encode_control


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
        if not self.vanished and not self.writer.is_closing():
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


@dataclass(frozen=True, slots=True)
class GilbertElliott:
    """Bursty loss: a two-state Markov chain, stepped once per datagram."""

    p: float
    """The chance of going from the good state to the bad one."""
    r: float
    """The chance of going from the bad state back to the good one."""
    loss_good: float = 0.0
    """The chance of losing a datagram in the good state."""
    loss_bad: float = 1.0
    """The chance of losing a datagram in the bad state."""


class UdpFaults:
    """Faults on the datagrams one way. Seeded: a run with the same seed loses the same
    datagrams, given the same traffic."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self.loss = 0.0
        """The chance of losing each datagram: Bernoulli loss."""
        self.bursts: GilbertElliott | None = None
        """Bursty loss, on top of `loss`."""
        self.delay_ms = 0.0
        """Every datagram arrives this much later."""
        self.jitter_ms = 0.0
        """Up to this much more delay, uniform, per datagram. Datagrams keep their order,
        as they do on one WiFi link."""
        self.duplicate = 0.0
        """The chance of each datagram arriving twice."""
        self.dropped = 0
        """Datagrams the faults lost."""
        self._drops: dict[int | None, set[int]] = {}
        self._bad = False
        """The Gilbert-Elliott state."""
        self._blackout_until = 0.0
        self._delayed: deque[tuple[float, Callable[[], None]]] = deque()
        """Datagrams on their way, in the order they arrive."""
        self._timer: asyncio.TimerHandle | None = None

    def drop(self, stream: int | None, seqs: Iterable[int]) -> None:
        """Lose the stream frames with these `seqs`: of stream id `stream`, or of any stream."""
        self._drops.setdefault(stream, set()).update(seqs)

    def blackout(self, ms: float) -> None:
        """Lose every datagram for the next `ms`."""
        self._blackout_until = asyncio.get_running_loop().time() + ms / 1000

    def apply(self, data: bytes, deliver: Callable[[bytes], None]) -> None:
        """Deliver `data` once, twice, later or never, as the faults say."""
        now = asyncio.get_running_loop().time()
        if self._lost(data, now):
            self.dropped += 1
            return
        copies = 2 if self.duplicate and self._rng.random() < self.duplicate else 1
        delay = self.delay_ms + (self._rng.uniform(0, self.jitter_ms) if self.jitter_ms else 0)
        due = now + delay / 1000
        if self._delayed:
            due = max(due, self._delayed[-1][0])  # no overtaking
        elif due <= now:
            for _ in range(copies):
                deliver(data)
            return
        for _ in range(copies):
            self._delayed.append((due, lambda: deliver(data)))
        self._arm()

    def _lost(self, data: bytes, now: float) -> bool:
        # The burst state steps on every datagram, whatever else befalls it.
        burst = False
        if self.bursts is not None:
            flip = self.bursts.r if self._bad else self.bursts.p
            if self._rng.random() < flip:
                self._bad = not self._bad
            loss = self.bursts.loss_bad if self._bad else self.bursts.loss_good
            burst = self._rng.random() < loss
        if now < self._blackout_until or burst:
            return True
        if self._drops and _frame_in(data, self._drops):
            return True
        return bool(self.loss) and self._rng.random() < self.loss

    def _arm(self) -> None:
        if self._timer is None and self._delayed:
            self._timer = asyncio.get_running_loop().call_at(self._delayed[0][0], self._arrive)

    def _arrive(self) -> None:
        """Deliver, in order, every datagram due by now."""
        self._timer = None
        now = asyncio.get_running_loop().time()
        while self._delayed and self._delayed[0][0] <= now:
            self._delayed.popleft()[1]()
        self._arm()


def _frame_in(data: bytes, drops: dict[int | None, set[int]]) -> bool:
    try:
        frame = decode_datagram(data)
    except WireError:
        return False
    if not isinstance(frame, StreamFrame):
        return False
    return frame.seq in drops.get(frame.stream, ()) or frame.seq in drops.get(None, ())


class Faults:
    """A device's faults: on its TCP Connection, where they act on the Connection of the
    moment, on its datagrams each way, and on its mic."""

    def __init__(
        self,
        down_rng: random.Random,
        up_rng: random.Random,
        mic_tx_resumed: Callable[[], None] = lambda: None,
    ) -> None:
        """`down_rng` and `up_rng` drive the UDP faults each way. `mic_tx_resumed()` is
        called when `mic_tx_stall` clears."""
        self.udp_down = UdpFaults(down_rng)
        """Faults on the datagrams from the hub."""
        self.udp_up = UdpFaults(up_rng)
        """Faults on the datagrams to the hub."""
        self._mic_tx_stall = False
        self._mic_tx_resumed = mic_tx_resumed
        self._connection: ConnectionSocket | None = None
        self._stall_reads = False
        self._reads_resumed = asyncio.Event()
        self._reads_resumed.set()
        self.delay_replies_ms = 0
        """The device holds each command's reply this long. It does the command at once."""
        self._failures: list[str] = []

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

    @property
    def mic_tx_stall(self) -> bool:
        """While set, the mic sends nothing, as if its UDP send task were stuck. Capture goes
        on into each stream's 200 ms queue, which drops its oldest frames on overflow and
        counts them in `dropped`. Clearing it sends what the queue holds, at once. A `stop`
        during a stall drops what's queued."""
        return self._mic_tx_stall

    @mic_tx_stall.setter
    def mic_tx_stall(self, stall: bool) -> None:
        self._mic_tx_stall = stall
        if not stall:
            self._mic_tx_resumed()

    def fail_next(self, code: str = "failed") -> None:
        """Fail the next command with error `code`, without doing it. Each call fails one more."""
        self._failures.append(code)

    def next_failure(self) -> str | None:
        """The error code to fail the command at hand with, if `fail_next` asked for one."""
        return self._failures.pop(0) if self._failures else None

    async def readable(self, connection: ConnectionSocket) -> None:
        """Return once the device may take in what it read: never, if it vanished."""
        while self._stall_reads or connection.vanished:
            if connection.vanished:
                await asyncio.Event().wait()
            await self._reads_resumed.wait()
