"""Each device's timeline, and the JSONL trace of every device's."""

import asyncio
import json
import time
from pathlib import Path

from tinyaisense_sim import SimDevice
from tinyaisense_sim.pytest_plugin import SimulatorFactory
from tinyaisense_wire import Welcome

from .raw_hub import CONN_ID, Online, RawHub, RawUdp

ECHO = "m5stack-atom-echo"


def summary(device: SimDevice) -> list[tuple[str, object]]:
    """Each entry's type, and what it's about."""
    about: list[tuple[str, object]] = []
    for entry in device.timeline:
        match entry.type:
            case "tx" | "rx":
                about.append((entry.type, entry.data["msg"]["type"]))
            case "status":
                about.append(("status", (entry.data["state"], entry.data["status"])))
            case "edge":
                about.append(("edge", (entry.data["cap"], entry.data["edge"])))
            case _:
                about.append((entry.type, entry.data["fault"]))
    return about


async def test_the_timeline_holds_what_went_in_and_out_and_what_befell_the_device(
    online: Online,
) -> None:
    device = online.device
    await online.ok("led", "set", {"color": "#ff0000"})
    device.button.press()
    device.faults.vanish()

    assert summary(device) == [
        ("status", ("connecting", "connecting")),
        ("tx", "hello"),
        ("rx", "welcome"),
        ("status", ("online", "no_agent")),
        ("rx", "cmd"),
        ("tx", "reply"),
        ("status", ("online", "claim")),
        ("edge", ("button", "down")),
        ("tx", "event"),
        ("fault", "vanish"),
    ]


async def test_timeline_entries_are_stamped_in_epoch_us_and_carry_whole_messages(
    online: Online,
) -> None:
    before = time.time_ns() // 1000
    result = await online.ok("mic", "stop")

    *_, rx, tx = online.device.timeline
    assert before <= rx.t <= tx.t <= time.time_ns() // 1000
    assert rx.data == {"msg": {"type": "cmd", "id": 1, "cap": "mic", "name": "stop", "args": {}}}
    assert tx.data == {"msg": {"type": "reply", "id": 1, "ok": True, "result": result}}


async def test_a_trace_file_gets_every_devices_timeline_as_json_lines(
    raw_hub: RawHub, simulator: SimulatorFactory, tmp_path: Path
) -> None:
    trace = tmp_path / "trace.jsonl"
    udp = await RawUdp.open()
    sim = simulator(raw_hub.address, trace=trace)
    devices: list[SimDevice] = []
    for name in ("kitchen", "den"):
        added = asyncio.create_task(sim.add(ECHO, name))
        connection = await raw_hub.accept()
        connection.send(Welcome(name, CONN_ID, udp.port))
        devices.append(await added)
    devices[0].button.press()
    await sim.close()
    udp.close()

    lines = [json.loads(line) for line in trace.read_text().splitlines()]
    for device in devices:
        assert [line for line in lines if line["device"] == device.instance] == [
            {"t": entry.t, "device": device.instance, "type": entry.type} | entry.data
            for entry in device.timeline
        ]
    assert [line["t"] for line in lines] == sorted(line["t"] for line in lines)
