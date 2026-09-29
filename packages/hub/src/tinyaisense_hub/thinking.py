"""The hub layer's `thinking` on one device: docs/protocol/kinds/device.md#status. Sans-IO.

It shows once an Utterance from the device is delivered to the agent, and ends when the agent
moves on the device, starting its speaker or a listen, or after `timeout` µs.
"""

from dataclasses import dataclass
from typing import Literal

from .keepalive import Tick


@dataclass(frozen=True, slots=True)
class Delivered:
    """An Utterance from the device went to the agent."""


@dataclass(frozen=True, slots=True)
class Moved:
    """The agent started the device's speaker, or a listen on it."""


@dataclass(frozen=True, slots=True)
class Show:
    """Set the hub layer of the device's Status."""

    state: Literal["thinking", "agent"]


class Thinking:
    def __init__(self, *, timeout: int) -> None:
        self._timeout = timeout
        self._until: int | None = None
        """While it shows."""

    def handle(self, input: Delivered | Moved | Tick, now: int) -> list[Show]:
        showing = self._until is not None
        match input:
            case Delivered():
                self._until = now + self._timeout
                return [] if showing else [Show("thinking")]
            case Moved():
                self._until = None
            case Tick():
                if self._until is not None and now >= self._until:
                    self._until = None
        return [Show("agent")] if showing and self._until is None else []

    def next_deadline(self) -> int | None:
        return self._until
