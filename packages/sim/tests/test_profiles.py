import tinyaisense_sim
from tinyaisense_sim import PROFILES, device_id_for
from tinyaisense_wire import Manifest

PCM16 = {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": [10, 20]}
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
    manifest = PROFILES["m5stack-atom-echo"]("023171d89ad0", "sim-x")

    assert manifest.to_json() == ECHO_MANIFEST
    assert manifest == Manifest.from_json(ECHO_MANIFEST)


def test_device_ids_are_locally_administered_and_stable_per_instance_name() -> None:
    assert device_id_for("kitchen") == "023171d89ad0"
    assert device_id_for("den") == "028a8a1a9fe8"


def test_firmware_is_sim_and_the_package_version() -> None:
    assert f"sim-{tinyaisense_sim.__version__}" == tinyaisense_sim.FIRMWARE
