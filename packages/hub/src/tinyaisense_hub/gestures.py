"""The gestures of one button, derived from its raw edges: docs/protocol/kinds/button.md.
Sans-IO.

Every time is in µs on the hub's monotonic clock. An edge's time is its device `ts`, mapped
through the Connection's clock offset, so that network delay doesn't stretch a press.
"""

from dataclasses import dataclass
from typing import Literal

from .keepalive import Tick


@dataclass(frozen=True, slots=True)
class Down:
    """The button went down at `ts`."""

    ts: int


@dataclass(frozen=True, slots=True)
class Up:
    """The button went up at `ts`."""

    ts: int


@dataclass(frozen=True, slots=True)
class Gesture:
    name: Literal["click", "double_click", "long_press"]
    ts: int
    """When it became one."""


class Gestures:
    def __init__(self, *, click: int, double_click: int, long_press: int) -> None:
        """A press shorter than `click` is a click. A second click whose press starts within
        `double_click` of the first one's release is also a double click. A press held for
        `long_press` is a long press, and no click."""
        self._click = click
        self._double_click = double_click
        self._long_press = long_press
        self._down: int | None = None
        """While the button is down: when it went down."""
        self._long_pressed = False
        """Whether the press it's down for is a long press yet."""
        self._clicked: int | None = None
        """When the last click ended, if a double click may yet follow it."""
        self._second = False
        """Whether the press it's down for may be the second click of a double click."""
        self._latest: int | None = None
        """When the latest edge happened. An edge older than that came in too late to count."""

    def handle(self, input: Down | Up | Tick, now: int) -> list[Gesture]:
        if isinstance(input, Down | Up):
            if self._latest is not None and input.ts < self._latest:
                return []
            self._latest = input.ts
        match input:
            case Down(ts):
                self._down = ts
                self._long_pressed = False
                self._second = self._clicked is not None and ts - self._clicked < self._double_click
                self._clicked = None
            case Up(ts):
                down, self._down = self._down, None
                if down is None or self._long_pressed:
                    return []
                if ts - down >= self._long_press:  # its deadline hasn't come in yet
                    return [Gesture("long_press", down + self._long_press)]
                if ts - down >= self._click:
                    return []
                if self._second:
                    return [Gesture("click", ts), Gesture("double_click", ts)]
                self._clicked = ts
                return [Gesture("click", ts)]
            case Tick():
                if (at := self.next_deadline()) is not None and now >= at:
                    self._long_pressed = True
                    return [Gesture("long_press", at)]
        return []

    def next_deadline(self) -> int | None:
        if self._down is None or self._long_pressed:
            return None
        return self._down + self._long_press
