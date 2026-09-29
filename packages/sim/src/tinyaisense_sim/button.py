"""The button Kind on a simulated device: docs/protocol/kinds/button.md."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from tinyaisense_wire import AudioFormat

type Edge = Literal["down", "up"]

CLICK_MS = 50
"""How long `click` holds the button down."""
DOUBLE_CLICK_GAP_MS = 100
"""How long `double_click` leaves between its clicks."""
LONG_PRESS_MS = 1500
"""How long `long_press` holds the button down: past the hub's default 1 s."""
FIRST_STREAM = 128
LAST_STREAM = 255
"""The device-range stream ids, which the Reflex opens mics on."""


@dataclass(frozen=True, slots=True)
class Reflex:
    """What an armed button does on `down`, by itself: cut the `stop` speakers, then open the
    `start` mic, in its format."""

    stop: tuple[str, ...]
    start: tuple[str, AudioFormat] | None


class SimButton:
    """One button capability. Its edges go out as `down` and `up` Events while the device is
    online, each on TCP and mirrored twice over UDP."""

    def __init__(self, edge: Callable[[Edge], None]) -> None:
        """`edge` sends each edge."""
        self._edge = edge
        self.pressed = False
        """Whether the button is down."""

    def press(self) -> None:
        """Push the button down. It bounces on real hardware, but not here: pushing it while
        it's down does nothing."""
        if not self.pressed:
            self.pressed = True
            self._edge("down")

    def release(self) -> None:
        """Let the button go. Letting go of a button that isn't down does nothing."""
        if self.pressed:
            self.pressed = False
            self._edge("up")

    async def click(self, ms: float = CLICK_MS) -> None:
        """Press the button, and release it `ms` later."""
        self.press()
        await asyncio.sleep(ms / 1000)
        self.release()

    async def double_click(self, ms: float = CLICK_MS, gap_ms: float = DOUBLE_CLICK_GAP_MS) -> None:
        """Click twice, `gap_ms` apart."""
        await self.click(ms)
        await asyncio.sleep(gap_ms / 1000)
        await self.click(ms)

    async def long_press(self, ms: float = LONG_PRESS_MS) -> None:
        """Press the button, and hold it `ms`: past the hub's long press, by default."""
        await self.click(ms)
