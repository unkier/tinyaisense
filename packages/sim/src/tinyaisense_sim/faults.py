"""Faults a simulated device can suffer. Each is live: set or trigger it at any time."""

import asyncio
import random
import socket
import struct
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import cast

from tinyaisense_wire import (
    Frame,
    FrameType,
    JsonObject,
    Message,
    StreamFrame,
    WireError,
    decode_datagram,
    encode_control,
    encode_frame,
)

from .clock import DeviceClock

GARBAGE = encode_frame(Frame(FrameType.JSON, b"\xffno json"))
"""What `send_garbage` sends by default: a JSON frame whose body isn't JSON, nor even UTF-8."""
REBOOT_MS = 2000
"""How long a reboot keeps the device down: about what an Echo takes to boot and rejoin WiFi."""

type Noted = Callable[[str, JsonObject], None]
"""`noted(fault, args)`: a fault struck, or was set or cleared."""


class ConnectionSocket:
    """One Connection's TCP socket, as the device and its faults see it."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        sent: Callable[[Message], None],
    ) -> None:
        """`sent(message)` is called as each message goes out."""
        self.reader = reader
        self.writer = writer
        self._sent = sent
        self.vanished = False
        """Nothing goes out or comes in any more, and the device never closes it."""
        self._later: deque[tuple[float, Message]] = deque()
        """What `send_later` holds, in the order it goes out, with when."""
        self._later_timer: asyncio.TimerHandle | None = None

    @property
    def raw(self) -> socket.socket:
        return cast(socket.socket, self.writer.get_extra_info("socket"))

    def send(self, message: Message) -> None:
        if self.send_bytes(encode_control(message)):
            self._sent(message)

    def send_bytes(self, data: bytes) -> bool:
        """Whether it went out."""
        if self.vanished or self.writer.is_closing():
            return False
        self.writer.write(data)
        return True

    def send_later(self, message: Message, delay: float) -> None:
        """Send `message` `delay` s from now, but never ahead of one sent this way before."""
        loop = asyncio.get_running_loop()
        due = loop.time() + delay
        if not self._later and delay <= 0:
            self.send(message)
            return
        if self._later:
            due = max(due, self._later[-1][0])
        self._later.append((due, message))
        if self._later_timer is None:
            self._later_timer = loop.call_at(self._later[0][0], self._send_due)

    def _send_due(self) -> None:
        self._later_timer = None
        loop = asyncio.get_running_loop()
        while self._later and self._later[0][0] <= loop.time():
            self.send(self._later.popleft()[1])
        if self._later:
            self._later_timer = loop.call_at(self._later[0][0], self._send_due)

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

    def __init__(self, rng: random.Random, noted: Noted) -> None:
        self._rng = rng
        self._noted = noted
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
        self.link_down = False
        """No link: every datagram is lost, those on their way included. A WiFi drop sets it
        both ways."""
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
        seqs = set(seqs)
        self._drops.setdefault(stream, set()).update(seqs)
        self._noted("drop", {"stream": stream, "seqs": sorted(seqs)})

    def blackout(self, ms: float) -> None:
        """Lose every datagram for the next `ms`."""
        self._blackout_until = asyncio.get_running_loop().time() + ms / 1000
        self._noted("blackout", {"ms": ms})

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
        if self.link_down or now < self._blackout_until or burst:
            return True
        if self._drops and _frame_in(data, self._drops):
            return True
        return bool(self.loss) and self._rng.random() < self.loss

    def _arm(self) -> None:
        if self._timer is None and self._delayed:
            self._timer = asyncio.get_running_loop().call_at(self._delayed[0][0], self._arrive)

    def lose_in_flight(self) -> None:
        """The device's network stack went down: the datagrams on their way are lost."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.dropped += len(self._delayed)
        self._delayed.clear()

    def _arrive(self) -> None:
        """Deliver, in order, every datagram due by now."""
        self._timer = None
        now = asyncio.get_running_loop().time()
        while self._delayed and self._delayed[0][0] <= now:
            deliver = self._delayed.popleft()[1]
            if self.link_down:
                self.dropped += 1
            else:
                deliver()
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
    moment, on its datagrams each way, on its clock and on its mic."""

    def __init__(
        self,
        down_rng: random.Random,
        up_rng: random.Random,
        clock: DeviceClock,
        *,
        mic_tx_resumed: Callable[[], None],
        wifi_changed: Callable[[], None],
        reboot: Callable[[float, str], None],
        noted: Noted,
    ) -> None:
        """`down_rng` and `up_rng` drive the UDP faults each way. `mic_tx_resumed()` is
        called when `mic_tx_stall` clears, and `wifi_changed()` when `wifi_down` changes.
        `reboot(down_ms, reason)` reboots the device. `noted(fault, args)` notes each fault
        that strikes, or is set or cleared, on the device's timeline."""
        self._noted = noted
        self._clock = clock
        self._wifi_changed = wifi_changed
        self._wifi_down = False
        self._wifi_timer: asyncio.TimerHandle | None = None
        """Brings WiFi back, after `wifi_drop(ms)`."""
        self._reboot = reboot
        self.clock_offset_us = 0
        """What the device clock reads at boot, from the next `reboot` on: real firmware's
        starts at 0, and never steps."""
        self.udp_down = UdpFaults(
            down_rng, lambda fault, args: noted(fault, {"way": "down"} | args)
        )
        """Faults on the datagrams from the hub."""
        self.udp_up = UdpFaults(up_rng, lambda fault, args: noted(fault, {"way": "up"} | args))
        """Faults on the datagrams to the hub."""
        self._mic_tx_stall = False
        self._mic_tx_resumed = mic_tx_resumed
        self._connection: ConnectionSocket | None = None
        self._stall_reads = False
        self._reads_resumed = asyncio.Event()
        self._reads_resumed.set()
        self.delay_replies_ms = 0
        """The device holds each command's reply this long. It does the command at once."""
        self.delay_events_ms = 0
        """The device holds each Event's TCP copy this long, as a stalled TCP send would,
        keeping their order. The UDP mirrors of button Events go out at once."""
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
        self._noted("rst", {})
        if self._connection is not None and not self._connection.vanished:
            self._connection.reset()

    def close(self) -> None:
        """Close the Connection cleanly: the hub gets a FIN."""
        self._noted("close", {})
        if self._connection is not None and not self._connection.vanished:
            self._connection.close()

    def vanish(self) -> None:
        """Go silent on the Connection both ways, like a device whose WiFi died.

        The device gives up on it after its own dead timeout, then reconnects. The hub is
        never told: it has to notice the silence.
        """
        self._noted("vanish", {})
        if self._connection is not None:
            self._connection.vanished = True

    def send_garbage(self, data: bytes = GARBAGE) -> None:
        """Send `data` on the Connection, whatever it is: by default, a protocol error that
        makes the hub close the Connection."""
        self._noted("send_garbage", {"bytes": len(data)})
        if self._connection is not None:
            self._connection.send_bytes(data)

    @property
    def stall_reads(self) -> bool:
        """While set, the device reads nothing from the hub. What the hub sends backs up
        into TCP, and what had already come in waits."""
        return self._stall_reads

    @stall_reads.setter
    def stall_reads(self, stall: bool) -> None:
        self._noted("stall_reads", {"on": stall})
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
    def drift_ppm(self) -> float:
        """How fast the device's crystal runs against true time, in parts per million: negative
        is slow. It clocks the device clock, so every `ts`, and the mic and speaker alike. A
        change keeps the clock's reading, and lasts across reboots."""
        return self._clock.ppm

    @drift_ppm.setter
    def drift_ppm(self, ppm: float) -> None:
        self._noted("drift_ppm", {"ppm": ppm})
        self._clock.ppm = ppm

    @property
    def mic_tx_stall(self) -> bool:
        """While set, the mic sends nothing, as if its UDP send task were stuck. Capture goes
        on into each stream's 200 ms queue, which drops its oldest frames on overflow and
        counts them in `dropped`. Clearing it sends what the queue holds, at once. A `stop`
        during a stall drops what's queued."""
        return self._mic_tx_stall

    @mic_tx_stall.setter
    def mic_tx_stall(self, stall: bool) -> None:
        self._noted("mic_tx_stall", {"on": stall})
        self._mic_tx_stall = stall
        if not stall:
            self._mic_tx_resumed()

    @property
    def wifi_down(self) -> bool:
        """While set, the device has no WiFi, and shows `offline`. It gives up on its
        Connection at once, which tells the hub nothing, as with `vanish`, and tries no other
        until WiFi is back. Every datagram either way is lost."""
        return self._wifi_down

    @wifi_down.setter
    def wifi_down(self, down: bool) -> None:
        if self._wifi_timer is not None:
            self._wifi_timer.cancel()
            self._wifi_timer = None
        if down != self._wifi_down:
            self._noted("wifi_down", {"on": down})
            self._wifi_down = self.udp_down.link_down = self.udp_up.link_down = down
            self._wifi_changed()

    def wifi_drop(self, ms: float | None = None) -> None:
        """Drop WiFi: for `ms`, or until `wifi_down` is cleared."""
        self.wifi_down = True
        if ms is not None:
            self._wifi_timer = asyncio.get_running_loop().call_later(ms / 1000, self._wifi_back)

    def power_off(self) -> None:
        """The device is off for good: a `wifi_drop(ms)` never ends."""
        if self._wifi_timer is not None:
            self._wifi_timer.cancel()
            self._wifi_timer = None

    def _wifi_back(self) -> None:
        self._wifi_timer = None
        self.wifi_down = False

    def reboot(self, down_ms: float = REBOOT_MS, reason: str = "sw") -> None:
        """Reboot the device, which is down for `down_ms`, then boots afresh: its clock from
        `clock_offset_us`, its volume from the Board's default, and `diag` with `reason` as
        its `reset_reason`. Its token survives.

        Its network stack goes down with it, so the hub isn't told: the device sends nothing
        more on the Connection, and the new stack resets it as it boots. Its UDP socket comes
        back on another port.
        """
        self._noted("reboot", {"down_ms": down_ms, "reason": reason})
        self._reboot(down_ms, reason)

    def fail_next(self, code: str = "failed") -> None:
        """Fail the next command with error `code`, without doing it. Each call fails one more."""
        self._noted("fail_next", {"code": code})
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
