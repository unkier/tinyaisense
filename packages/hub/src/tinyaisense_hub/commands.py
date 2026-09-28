"""Command timeouts for one device Connection. Sans-IO.

Every time is in µs on the hub's monotonic clock. Timeouts are enforced on the hub only: a
command without a reply by its deadline fails with `timeout`, and a late reply is dropped.
"""

from dataclasses import dataclass

from .keepalive import Tick


@dataclass(frozen=True, slots=True)
class Sent:
    """A `cmd` went out, due a reply within `timeout`."""

    id: int
    timeout: int


@dataclass(frozen=True, slots=True)
class Replied:
    """A `reply` with this `id` arrived."""

    id: int


@dataclass(frozen=True, slots=True)
class Answered:
    """The reply came in time: deliver it."""

    id: int


@dataclass(frozen=True, slots=True)
class TimedOut:
    """No reply in time: the command fails with `timeout`."""

    id: int


class CommandTimeouts:
    """The deadlines of the commands in flight on one Connection."""

    def __init__(self) -> None:
        self._deadlines: dict[int, int] = {}
        """Command id → when its reply is due, in the order the commands went out."""

    def handle(self, input: Sent | Replied | Tick, now: int) -> list[Answered | TimedOut]:
        match input:
            case Sent(id, timeout):
                self._deadlines[id] = now + timeout
                return []
            case Replied(id):
                deadline = self._deadlines.pop(id, None)
                if deadline is None:
                    return []  # late, or never sent: dropped
                return [Answered(id) if now < deadline else TimedOut(id)]
            case Tick():
                due = [id for id, deadline in self._deadlines.items() if deadline <= now]
                for id in due:
                    del self._deadlines[id]
                return [TimedOut(id) for id in due]

    def next_deadline(self) -> int | None:
        """When to `handle(Tick())` next; None with nothing in flight."""
        return min(self._deadlines.values(), default=None)
