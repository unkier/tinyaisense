"""Keepalive, dead detection and clock sync for one device Connection. Sans-IO.

Every time is in µs on the hub's monotonic clock, the one `ping.t` carries.
"""

from dataclasses import dataclass

from tinyaisense_wire import Pong


@dataclass(frozen=True, slots=True)
class Inbound:
    """A frame arrived from the device."""


@dataclass(frozen=True, slots=True)
class Tick:
    """`next_deadline()` has passed."""


@dataclass(frozen=True, slots=True)
class SendPing:
    t: int


@dataclass(frozen=True, slots=True)
class Dead:
    """The device has been silent for too long: close the Connection."""


class Keepalive:
    """Pings every `ping_interval` from the `hello` on; dead after `dead_after` of silence."""

    def __init__(self, now: int, *, ping_interval: int, dead_after: int) -> None:
        """`now` is when the `hello` arrived. The first ping is due at once."""
        self._ping_interval = ping_interval
        self._dead_after = dead_after
        self._next_ping = now
        self._last_inbound = now
        self._dead = False

    def handle(self, input: Inbound | Tick, now: int) -> list[SendPing | Dead]:
        if self._dead:
            return []
        if isinstance(input, Inbound):
            self._last_inbound = now
            return []
        if now >= self._last_inbound + self._dead_after:
            self._dead = True
            return [Dead()]
        if now >= self._next_ping:
            self._next_ping = now + self._ping_interval
            return [SendPing(now)]
        return []

    @property
    def last_inbound(self) -> int:
        return self._last_inbound

    def next_deadline(self) -> int | None:
        """When to `handle(Tick())` next; None once dead."""
        if self._dead:
            return None
        return min(self._next_ping, self._last_inbound + self._dead_after)


@dataclass(frozen=True, slots=True)
class _Sample:
    received: int
    rtt: int
    offset: int


class ClockSync:
    """The device clock's offset from the hub's: a min-RTT filter over recent pongs.

    A pong's device `ts` is taken to fall at the midpoint of its round trip, so a sample is
    off by at most half its RTT. The shortest round trip within `window` wins. It sets no
    timers, so unlike the other cores it has no actions and no `next_deadline()`.
    """

    def __init__(self, *, window: int) -> None:
        self._window = window
        self._samples: list[_Sample] = []
        self._best: _Sample | None = None

    def handle(self, pong: Pong, now: int) -> None:
        rtt = now - pong.t
        if rtt < 0:
            return  # not a `t` we sent
        self._samples = [s for s in self._samples if s.received > now - self._window]
        self._samples.append(_Sample(now, rtt, pong.ts - (pong.t + rtt // 2)))
        self._best = min(self._samples, key=lambda s: s.rtt)

    @property
    def offset(self) -> int | None:
        """Device clock minus hub clock; None before the first pong."""
        return None if self._best is None else self._best.offset

    def to_hub(self, ts: int) -> int | None:
        """A device `ts` on the hub's clock."""
        return None if self._best is None else ts - self._best.offset
