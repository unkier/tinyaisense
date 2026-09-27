"""Board profiles: the Manifest each simulated Board sends."""

from collections.abc import Callable

from tinyaisense_wire import PROTOCOL, Capability, JsonObject, Manifest


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


PROFILES: dict[str, Callable[[str, str], Manifest]] = {"m5stack-atom-echo": atom_echo}
"""Board → a function of `(device_id, firmware)` that builds its Manifest."""
