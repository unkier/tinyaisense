"""Loop timers for the sans-IO cores, on the hub's monotonic clock in µs."""

import asyncio
import time
from collections.abc import Callable


class Alarm:
    """One loop timer, set for the earliest deadline asked of it.

    Times are µs on the hub's monotonic clock. A deadline that moves later leaves the timer
    early: it finds nothing due and asks for the next one.
    """

    def __init__(self, due: Callable[[int], None]) -> None:
        self._due = due
        self._timer: asyncio.TimerHandle | None = None
        self._at = 0

    def set(self, deadline: int | None, now: int) -> None:
        if deadline is not None and (self._timer is None or deadline < self._at):
            self.cancel()
            self._at = deadline
            self._timer = asyncio.get_running_loop().call_later(
                max(0, deadline - now) / 1_000_000, self._fire
            )

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _fire(self) -> None:
        self._timer = None
        self._due(now_us())


def now_us() -> int:
    """The hub's monotonic clock, as `ping.t` carries it."""
    return time.monotonic_ns() // 1000
