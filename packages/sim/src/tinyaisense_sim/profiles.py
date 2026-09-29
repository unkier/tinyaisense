"""Board profiles: the Manifest each simulated Board sends, and how its hardware behaves."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from tinyaisense_wire import PROTOCOL, Capability, JsonObject, Manifest


@dataclass(frozen=True, slots=True)
class Health:
    """What `diag` reports of a Board's hardware, which the Simulator doesn't model."""

    heap_free: int
    heap_min: int
    rssi: int
    stack_hwm: Mapping[str, int]
    """Bytes, by task."""


@dataclass(frozen=True, slots=True)
class Board:
    manifest: Callable[[str, str], Manifest]
    """A function of `(device_id, firmware)` that builds the Board's Manifest."""
    led_brightness_cap: int
    """What LED brightness 100 maps to, 0 to 255."""
    default_volume: int
    """The speaker volume the device boots at, 0 to 100."""
    health: Health


def _pcm16() -> JsonObject:
    return {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": [10, 20]}


def atom_echo(device_id: str, firmware: str) -> Manifest:
    """The exact Atom Echo Manifest of docs/protocol/device.md."""
    return Manifest(
        PROTOCOL,
        device_id,
        "m5stack-atom-echo",
        firmware,
        (
            Capability("mic", "mic", {"formats": [_pcm16()]}),
            Capability("speaker", "speaker", {"formats": [_pcm16()]}),
            Capability("button", "button"),
            Capability("led", "led", {"count": 1}),
        ),
        (("mic", "speaker"),),
    )


PROFILES: dict[str, Board] = {
    # The firmware's Board file caps the Echo's LED the same way.
    "m5stack-atom-echo": Board(
        atom_echo,
        led_brightness_cap=64,
        default_volume=80,
        # ~180 KB of heap, less the firmware's ~68 KB, and a stack per firmware task.
        health=Health(
            heap_free=112_640,
            heap_min=98_304,
            rssi=-61,
            stack_hwm={
                "reflex": 1480,
                "spk": 2104,
                "mic": 1872,
                "udp_rx": 1536,
                "udp_tx": 1424,
                "ctrl": 2968,
                "led": 912,
            },
        ),
    ),
}
"""Every simulated Board, by name."""
