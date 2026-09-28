"""The Status a simulated device shows: docs/protocol/kinds/device.md#status."""

from dataclasses import dataclass
from typing import Literal

type HubStatus = Literal["no_agent", "agent", "thinking"]
"""The hub layer, which the hub sets with `status`."""

type Status = Literal[
    "identify",
    "listening",
    "claim",
    "offline",
    "connecting",
    "pending",
    "no_config",
    "speaking",
    "thinking",
    "no_agent",
    "agent",
]
"""What the device shows. `claim` is an agent's LED claim: the LED shows what the agent set."""


@dataclass(slots=True)
class StatusLayers:
    """Every state that applies to the device now, layer by layer."""

    identify: bool = False
    listening: bool = False
    """A mic stream is open."""
    claimed: bool = False
    """An agent has claimed the Status LED."""
    offline: bool = False
    """No WiFi."""
    connecting: bool = False
    """WiFi up, no hub yet."""
    pending: bool = False
    no_config: bool = False
    """No WiFi credentials."""
    speaking: bool = False
    """A speaker stream is open."""
    hub: HubStatus = "no_agent"

    @property
    def shown(self) -> Status:
        """The highest-priority state that applies."""
        states: list[tuple[bool, Status]] = [
            (self.identify, "identify"),
            (self.listening, "listening"),
            (self.claimed, "claim"),
            (self.offline, "offline"),
            (self.connecting, "connecting"),
            (self.pending, "pending"),
            (self.no_config, "no_config"),
            (self.speaking, "speaking"),
        ]
        for applies, state in states:
            if applies:
                return state
        return self.hub
