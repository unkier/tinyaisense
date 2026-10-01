"""The loop-lag warning: a timer every 10 ms, and a log line when it runs late."""

import logging
from dataclasses import dataclass

from .alarm import Alarm, now_us
from .keepalive import Tick
from .logs import fields

PERIOD = 10_000
"""How often the probe timer runs, in µs. A stall longer than this always makes it late."""
QUIET = 1_000_000
"""How long after a warning the next one waits, in µs. The lags in between are counted, and
come in that next one."""

_log = logging.getLogger("tinyaisense_hub.loop")


@dataclass(frozen=True, slots=True)
class Lagged:
    """The probe ran over the threshold late `count` times, by up to `max` µs."""

    count: int
    max: int


class LoopLag:
    """How late the probe timer runs, and when to warn of it. Sans-IO: times are µs on the
    hub's monotonic clock.

    A lag over the threshold is `Lagged` at once, unless a warning went out within `QUIET`:
    then it's counted, with those after it, into one `Lagged` once `QUIET` has passed.
    """

    def __init__(self, now: int, *, threshold: int) -> None:
        self._threshold = threshold
        self._due = now + PERIOD
        self._count = 0
        self._max = 0
        self._quiet_until = now

    def handle(self, input: Tick, now: int) -> list[Lagged]:
        if now < self._due:
            return []  # early
        if (lag := now - self._due) > self._threshold:
            self._count += 1
            self._max = max(self._max, lag)
        self._due = now + PERIOD
        if not self._count or now < self._quiet_until:
            return []
        lagged = Lagged(self._count, self._max)
        self._count = self._max = 0
        self._quiet_until = now + QUIET
        return [lagged]

    def next_deadline(self) -> int:
        return self._due


class LoopMonitor:
    """Warns, in the log, whenever the running loop lags over `threshold` µs."""

    def __init__(self, threshold: int) -> None:
        self._threshold = threshold
        self._lag: LoopLag | None = None
        self._alarm = Alarm(self._tick)

    def start(self) -> None:
        now = now_us()
        self._lag = LoopLag(now, threshold=self._threshold)
        self._alarm.set(self._lag.next_deadline(), now)

    def close(self) -> None:
        self._lag = None
        self._alarm.cancel()

    def _tick(self, now: int) -> None:
        if (lag := self._lag) is None:
            return
        for lagged in lag.handle(Tick(), now):
            max_ms = round(lagged.max / 1000, 1)
            _log.warning(
                "the loop lagged %d time(s), by up to %.1f ms",
                lagged.count,
                max_ms,
                extra=fields(type="loop.lag", count=lagged.count, max_ms=max_ms),
            )
        self._alarm.set(lag.next_deadline(), now)
