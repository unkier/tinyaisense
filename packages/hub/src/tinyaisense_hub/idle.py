"""The idle release of an MCP-only Session. Sans-IO.

The Session ends once `after` µs have passed with no tool call of it running.
"""

from dataclasses import dataclass

from .keepalive import Tick


@dataclass(frozen=True, slots=True)
class CallStarted:
    """A tool call started in the Session."""


@dataclass(frozen=True, slots=True)
class CallEnded:
    """A tool call in the Session ended."""


@dataclass(frozen=True, slots=True)
class Release:
    """End the Session: it's idle."""


class Idle:
    def __init__(self, *, after: int) -> None:
        self._after = after
        self._calls = 0
        self._until: int | None = None
        """While no call runs."""

    def handle(self, input: CallStarted | CallEnded | Tick, now: int) -> list[Release]:
        match input:
            case CallStarted():
                self._calls += 1
                self._until = None
            case CallEnded():
                self._calls -= 1
                if not self._calls:
                    self._until = now + self._after
            case Tick():
                if self._until is not None and now >= self._until:
                    self._until = None
                    return [Release()]
        return []

    def next_deadline(self) -> int | None:
        return self._until
