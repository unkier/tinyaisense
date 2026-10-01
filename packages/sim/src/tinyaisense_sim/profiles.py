"""Board profiles: the Manifest each simulated Board sends, and how its hardware behaves."""

import contextlib
import logging
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, replace

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


def sim_duplex(device_id: str, firmware: str) -> Manifest:
    """The Atom Echo's capabilities with no exclusive group: its mic and speaker run at once."""
    return replace(atom_echo(device_id, firmware), board="sim-duplex", exclusive=())


_ECHO = Board(
    atom_echo,
    # The firmware's Board file caps the Echo's LED the same way.
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
)

PROFILES: dict[str, Board] = {
    "m5stack-atom-echo": _ECHO,
    "sim-duplex": replace(_ECHO, manifest=sim_duplex),
}
"""Every simulated Board, by name."""

_PLACEHOLDER_ID = "020000000000"
"""The device_id a Manifest from `from_manifest` is checked with."""


def from_manifest(obj: JsonObject) -> Board:
    """A Board that sends the Manifest `obj`, as JSON: its capabilities may be of any Kind, and
    those of an unknown one answer every command with `unsupported`. Each device sends our
    `protocol`, and its own `device_id` and `firmware`, whatever `obj` says. The rest is as on
    the Echo, bar the LED, whose brightness has no cap.

    Raises `WireError` if `obj` isn't a Manifest.
    """
    obj = obj | {"protocol": PROTOCOL, "device_id": _PLACEHOLDER_ID, "firmware": "sim"}
    with _unlogged(logging.getLogger("tinyaisense_wire")):  # it's the hub's to ignore a Kind
        template = Manifest.from_json(obj)

    def manifest(device_id: str, firmware: str) -> Manifest:
        return replace(template, device_id=device_id, firmware=firmware)

    return replace(_ECHO, manifest=manifest, led_brightness_cap=255)


@contextlib.contextmanager
def _unlogged(logger: logging.Logger) -> Generator[None]:
    """Nothing `logger` logs gets out."""

    def quiet(record: logging.LogRecord) -> bool:
        return False

    logger.addFilter(quiet)
    try:
        yield
    finally:
        logger.removeFilter(quiet)
