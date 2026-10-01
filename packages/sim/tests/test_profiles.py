import pytest
import tinyaisense_sim
from tinyaisense_sim import PROFILES, device_id_for, from_manifest
from tinyaisense_wire import FailedReply, Manifest, WireError

from .conftest import WelcomeDevice

PCM16 = {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": [10, 20]}
FORMAT = {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": 10}
# The Atom Echo Manifest of docs/protocol/device.md, with this instance's identity.
ECHO_MANIFEST = {
    "protocol": 1,
    "device_id": "023171d89ad0",
    "board": "m5stack-atom-echo",
    "firmware": "sim-x",
    "capabilities": [
        {"id": "mic", "kind": "mic", "params": {"formats": [PCM16]}},
        {"id": "speaker", "kind": "speaker", "params": {"formats": [PCM16]}},
        {"id": "button", "kind": "button"},
        {"id": "led", "kind": "led", "params": {"count": 1}},
    ],
    "exclusive": [["mic", "speaker"]],
}


def test_the_echo_profile_sends_the_exact_echo_manifest() -> None:
    manifest = PROFILES["m5stack-atom-echo"].manifest("023171d89ad0", "sim-x")

    assert manifest.to_json() == ECHO_MANIFEST
    assert manifest == Manifest.from_json(ECHO_MANIFEST)


def test_device_ids_are_locally_administered_and_stable_per_instance_name() -> None:
    assert device_id_for("kitchen") == "023171d89ad0"
    assert device_id_for("den") == "028a8a1a9fe8"


def test_firmware_is_sim_and_the_package_version() -> None:
    assert f"sim-{tinyaisense_sim.__version__}" == tinyaisense_sim.FIRMWARE


def test_the_duplex_profile_is_the_echo_with_no_exclusive_group() -> None:
    manifest = PROFILES["sim-duplex"].manifest("023171d89ad0", "sim-x")

    assert manifest.to_json() == ECHO_MANIFEST | {"board": "sim-duplex", "exclusive": []}


THERMO = {
    "protocol": 1,
    "board": "thermo-1",
    "capabilities": [
        {"id": "temp", "kind": "thermometer", "params": {"unit": "C"}},
        {"id": "led", "kind": "led", "params": {"count": 3}},
    ],
    "exclusive": [],
}


def test_a_board_from_a_manifest_sends_it_with_each_devices_own_identity() -> None:
    board = from_manifest(THERMO | {"device_id": "0200000000ff", "firmware": "thermo-0.1"})

    manifest = board.manifest("023171d89ad0", "sim-x")

    assert manifest.to_json() == THERMO | {"device_id": "023171d89ad0", "firmware": "sim-x"}


def test_a_manifest_needs_no_protocol_device_id_or_firmware() -> None:
    board = from_manifest({k: v for k, v in THERMO.items() if k != "protocol"})

    assert board.manifest("023171d89ad0", "sim-x").protocol == 1


def test_a_manifest_that_isnt_one_is_refused() -> None:
    with pytest.raises(WireError) as refused:
        from_manifest(THERMO | {"exclusive": [["temp", "fan"]]})

    assert refused.value.code == "bad_message"


async def test_a_duplex_device_plays_and_captures_at_once(
    welcome: WelcomeDevice,
) -> None:
    online = await welcome("sim-duplex")

    await online.ok("speaker", "start", {"stream": 1, "format": FORMAT})
    await online.ok("mic", "start", {"stream": 2, "format": FORMAT})

    assert (online.device.speaker.stream, online.device.mic.stream) == (1, 2)


async def test_a_device_sends_the_manifest_its_board_was_made_from(
    welcome: WelcomeDevice,
) -> None:
    online = await welcome(from_manifest(THERMO))

    assert online.connection.hello is not None
    assert online.connection.hello.manifest == online.device.manifest
    assert online.device.manifest.board == "thermo-1"


async def test_a_capability_of_an_unknown_kind_answers_every_command_unsupported(
    welcome: WelcomeDevice,
) -> None:
    online = await welcome(from_manifest(THERMO))

    reply = await online.command("temp", "read")

    assert isinstance(reply, FailedReply)
    assert reply.code == "unsupported"
    assert await online.ok("led", "set", {"color": "#ff0000", "index": 2}) == {}
