"""The led Kind on a simulated device: docs/protocol/kinds/led.md."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from tinyaisense_wire import JsonObject

from .status import Status

type Pattern = Literal["solid", "blink", "pulse"]


@dataclass(frozen=True, slots=True)
class Light:
    """What one LED shows."""

    color: str
    """`#rrggbb`, in lowercase."""
    level: int
    """0 to 255: the brightness, scaled by the Board's cap."""
    pattern: Pattern = "solid"
    period_ms: int = 1000


OFF = Light("#000000", 0)

LOOKS: Mapping[Status, JsonObject] = {
    "identify": {"color": "#ffffff", "pattern": "blink", "period_ms": 250},
    "listening": {"color": "#00ff00"},
    "offline": {"color": "#ff0000", "pattern": "blink", "period_ms": 1000},
    "connecting": {"color": "#ffa000", "pattern": "blink", "period_ms": 500},
    "pending": {"color": "#ff00ff", "pattern": "pulse", "period_ms": 2000},
    "no_config": {"color": "#ff0000"},
    "speaking": {"color": "#0000ff"},
    "thinking": {"color": "#00ffff", "pattern": "pulse", "period_ms": 1000},
    "no_agent": {"color": "#ffa000", "brightness": 20},
    "agent": {"color": "#000000", "brightness": 0},
}
"""How the Status LED renders each Status, as `set` args. `claim` shows what the agent set."""


class SimLed:
    """One led capability: the agent's claim on it, if any, over what the device shows."""

    def __init__(self, count: int, brightness_cap: int) -> None:
        self.count = count
        self._brightness_cap = brightness_cap
        self.claim: list[Light] | None = None
        """What the agent set on each LED, while it holds a claim."""

    def set(self, args: JsonObject) -> None:
        """Raises `ValueError` for an `index` past the LED count."""
        index: int | None = args.get("index")
        if index is not None and index >= self.count:
            raise ValueError(f"index {index} is past the last LED, {self.count - 1}")
        claim = self.claim or [OFF] * self.count
        light = self.light(args)
        for i in range(self.count) if index is None else [index]:
            claim[i] = light
        self.claim = claim

    def off(self) -> None:
        self.claim = [OFF] * self.count

    def release(self) -> None:
        self.claim = None

    def lights(self, status: Status | None) -> tuple[Light, ...]:
        """What each LED shows, given the Status it shows: None if it shows none."""
        if status is not None and status != "claim":
            return (self.light(LOOKS[status]),) * self.count
        return tuple(self.claim or [OFF] * self.count)

    def light(self, args: Mapping[str, Any]) -> Light:
        """The Light that `set` args make, with the brightness cap applied."""
        brightness = cast(int, args.get("brightness", 100))
        return Light(
            cast(str, args["color"]).lower(),
            round(brightness * self._brightness_cap / 100),
            args.get("pattern", "solid"),
            args.get("period_ms", 1000),
        )
