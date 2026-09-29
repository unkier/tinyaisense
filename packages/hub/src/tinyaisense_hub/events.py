"""One Connection's device Events in order: docs/protocol/device.md#events. Sans-IO.

Each Event may come in more than once, by TCP and by its UDP mirrors. The first copy counts,
and later ones are dropped by `seq`. Events go on in `seq` order; one that comes in ahead of
a gap waits in the hold for it. Raw button edges skip the hold: they go on at once as `Raw`,
and again as `Ordered` in their turn.

Every time is in µs on the hub's monotonic clock.
"""

from collections.abc import Collection
from dataclasses import dataclass

from tinyaisense_wire import Event

from .keepalive import Tick


@dataclass(frozen=True, slots=True)
class Raw:
    """A raw button edge, as its first copy came in: perhaps ahead of `seq` order."""

    event: Event


@dataclass(frozen=True, slots=True)
class Ordered:
    """An Event in `seq` order."""

    event: Event


class EventOrder:
    def __init__(self, *, hold: int, buttons: Collection[str]) -> None:
        """An Event ahead of a gap waits up to `hold` for it. `buttons` are the capability ids
        of the Connection's buttons, whose raw edges skip the hold."""
        self._hold = hold
        self._buttons = buttons
        self._next = 0
        """The `seq` whose turn it is."""
        self._held: dict[int, tuple[Event, int]] = {}
        """`seq` → an Event ahead of a gap, and when it came in."""
        self._late: list[range] = []
        """The gaps given up on: `seq`s whose turn has passed, but which haven't come in."""

    def handle(self, input: Event | Tick, now: int) -> list[Raw | Ordered]:
        if isinstance(input, Tick):
            return self._give_up(now)
        event = input
        actions: list[Raw | Ordered] = [Raw(event)] if self._raw(event) else []
        if event.seq < self._next:
            return [*actions, Ordered(event)] if self._came_late(event.seq) else []
        if event.seq in self._held:
            return []
        if event.seq > self._next:
            self._held[event.seq] = (event, now)
            return actions
        actions.append(Ordered(event))
        self._next += 1
        return actions + self._release()

    def next_deadline(self) -> int | None:
        if not self._held:
            return None
        return min(arrived for _, arrived in self._held.values()) + self._hold

    def _give_up(self, now: int) -> list[Raw | Ordered]:
        """Every Event held for `hold` goes on, and so does all it waited behind: the gaps
        before it are given up on."""
        actions: list[Raw | Ordered] = []
        while self._held:
            seq, (_, arrived) = min(self._held.items(), key=lambda held: held[1][1])
            if now < arrived + self._hold:
                break
            for waiting in sorted(s for s in self._held if s <= seq):
                if waiting > self._next:
                    self._late.append(range(self._next, waiting))
                actions.append(Ordered(self._held.pop(waiting)[0]))
                self._next = waiting + 1
            actions += self._release()
        return actions

    def _release(self) -> list[Raw | Ordered]:
        """The held Events whose turn has come."""
        actions: list[Raw | Ordered] = []
        while (held := self._held.pop(self._next, None)) is not None:
            actions.append(Ordered(held[0]))
            self._next += 1
        return actions

    def _came_late(self, seq: int) -> bool:
        """Whether `seq` is from a gap given up on. If so, it's taken out of the gap."""
        for i, gap in enumerate(self._late):
            if seq in gap:
                self._late[i : i + 1] = [
                    r for r in (range(gap.start, seq), range(seq + 1, gap.stop)) if r
                ]
                return True
        return False

    def _raw(self, event: Event) -> bool:
        return event.cap in self._buttons and event.name in ("down", "up")
