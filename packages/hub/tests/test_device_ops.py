"""The owner's device ops: identify, diag and volume, through the owner socket."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
import tinyaisense
from tinyaisense_hub import Hub, HubError, OwnerClient, StateError, Timing
from tinyaisense_sim import Light, SimDevice, Simulator
from tinyaisense_sim.pytest_plugin import SimulatorFactory
from tinyaisense_wire import DEVICE_COMMANDS, Command, Hello, Reply, Welcome, validate

from .raw_device import RawDevice, echo_manifest

ECHO = "m5stack-atom-echo"
TOKEN = "agent-token"
"""The `start_hub` fixture's agent token."""

StartHub = Callable[..., Awaitable[Hub]]


async def paired(sim: Simulator, owner: OwnerClient, name: str = "kitchen") -> SimDevice:
    device = await sim.add(ECHO, name)
    await owner.approve(device.device_id, name)
    await device.wait_for(lambda: device.state == "online")
    return device


async def test_identify_blinks_the_device_white_at_4_hz_even_through_an_led_claim(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await agent.command("kitchen/led", "set", {"color": "#ff0000"})

        await owner.identify("kitchen")

        assert kitchen.status == "identify"
        # The Echo caps brightness 100 at 64 of 255.
        assert kitchen.lights() == (Light("#ffffff", 64, "blink", 250),)


async def test_identify_ends_after_its_seconds_and_the_device_shows_what_it_did_before(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await agent.command("kitchen/led", "set", {"color": "#ff0000"})
        await owner.identify("kitchen", 1)
        assert kitchen.status == "identify"

        async with asyncio.timeout(5):
            await kitchen.wait_for(lambda: kitchen.status != "identify")

        assert kitchen.lights() == (Light("#ff0000", 64),)


@pytest.mark.parametrize("seconds", [0, 301])
async def test_identify_takes_1_to_300_seconds(
    sim: Simulator, owner: OwnerClient, seconds: int
) -> None:
    kitchen = await paired(sim, owner)

    with pytest.raises(HubError) as refused:
        await owner.identify("kitchen", seconds)

    assert refused.value.code == "invalid_args"
    assert kitchen.status == "no_agent"


async def test_identify_of_an_offline_device_is_disconnected(
    sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    await kitchen.close()

    with pytest.raises(HubError) as refused:
        await owner.identify("kitchen")

    assert refused.value.code == "disconnected"


async def test_diag_reports_plausible_health_and_counts_reconnects(
    sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.rst()
    async with asyncio.timeout(5):
        await kitchen.wait_for(lambda: kitchen.reconnects == 1 and kitchen.state == "online")

    diag = await owner.diag("kitchen")

    validate(DEVICE_COMMANDS["diag"].result, diag, "diag")
    assert diag["reset_reason"] == "poweron"
    assert diag["reconnects"] == 1
    assert 0 < diag["heap_min"] <= diag["heap_free"] < 180_000  # the Echo has no PSRAM
    assert -100 < diag["rssi"] < 0
    assert {"ctrl", "spk", "mic", "reflex"} <= diag["stack_hwm"].keys()


async def test_volume_sets_the_speaker_level(sim: Simulator, owner: OwnerClient) -> None:
    kitchen = await paired(sim, owner)

    await owner.set_volume("kitchen", 40)

    assert kitchen.speaker.level == 40


async def test_the_hub_gives_a_rebooted_device_its_volume_back(
    sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    await owner.set_volume("kitchen", 40)

    await kitchen.close()  # power off: the device keeps no volume
    kitchen = await sim.add(ECHO, "kitchen")

    async with asyncio.timeout(5):
        await kitchen.wait_for(lambda: kitchen.speaker.level == 40)


async def test_the_volume_survives_a_hub_restart(
    start_hub: StartHub, simulator: SimulatorFactory, tmp_path: Path
) -> None:
    hub = await start_hub()
    kitchen = await simulator(hub.device_address, tmp_path / "sim").add(ECHO, "kitchen")
    async with OwnerClient(hub.owner_socket) as owner:
        await owner.approve(kitchen.device_id, "kitchen")
        await kitchen.wait_for(lambda: kitchen.state == "online")
        await owner.set_volume("kitchen", 25)
    await hub.close()

    hub = await start_hub()

    again = await simulator(hub.device_address, tmp_path / "sim").add(ECHO, "kitchen")
    async with asyncio.timeout(5):
        await again.wait_for(lambda: again.speaker.level == 25)


async def test_a_device_that_pairs_again_keeps_its_volume(
    hub: Hub, owner: OwnerClient, simulator: SimulatorFactory
) -> None:
    first = simulator(hub.device_address)
    await owner.set_volume((await paired(first, owner)).device_id, 30)
    await first.close()  # its token dies with it

    again = await simulator(hub.device_address).add(ECHO, "kitchen")
    await owner.approve(again.device_id)

    async with asyncio.timeout(5):
        await again.wait_for(lambda: again.speaker.level == 30)


async def test_the_volume_of_an_offline_device_applies_when_it_comes_back(
    sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    await kitchen.close()

    stored = await owner.set_volume("kitchen", 15)
    kitchen = await sim.add(ECHO, "kitchen")

    assert not stored["applied"]
    async with asyncio.timeout(5):
        await kitchen.wait_for(lambda: kitchen.speaker.level == 15)


@pytest.mark.parametrize("level", [-1, 101])
async def test_the_volume_is_0_to_100(sim: Simulator, owner: OwnerClient, level: int) -> None:
    kitchen = await paired(sim, owner)
    before = kitchen.speaker.level

    with pytest.raises(HubError) as refused:
        await owner.set_volume("kitchen", level)

    assert refused.value.code == "invalid_args"
    assert kitchen.speaker.level == before


async def test_a_volume_the_device_refuses_stays_stored_for_its_next_welcome(
    sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    await owner.diag("kitchen")  # a round trip, so the bring-up after `welcome` is over
    kitchen.faults.fail_next("busy")

    with pytest.raises(HubError) as refused:
        await owner.set_volume("kitchen", 20)

    assert refused.value.code == "busy"
    assert "stored" in refused.value.message
    kitchen.faults.rst()
    async with asyncio.timeout(5):
        await kitchen.wait_for(lambda: kitchen.speaker.level == 20)


async def test_a_hub_refuses_a_state_file_with_a_volume_past_100(
    start_hub: StartHub, simulator: SimulatorFactory, tmp_path: Path
) -> None:
    hub = await start_hub()
    async with OwnerClient(hub.owner_socket) as owner:
        await owner.set_volume((await paired(simulator(hub.device_address), owner)).device_id, 40)
    await hub.close()
    state = tmp_path / "state" / "state.json"
    state.write_text(state.read_text().replace('"volume": 40', '"volume": 140'))

    with pytest.raises(StateError):
        await start_hub()


@pytest.mark.parametrize(
    ("address", "name"),
    [
        ("kitchen", "identify"),
        ("kitchen/identify", "identify"),
        ("identify", "identify"),
        ("kitchen/diag", "diag"),
    ],
)
async def test_agents_cannot_reach_device_level_commands(
    hub: Hub, sim: Simulator, owner: OwnerClient, address: str, name: str
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.command(address, name)

    assert refused.value.code == "unknown_address"
    assert kitchen.status != "identify"


# The bring-up, seen by a hand-rolled device that replies when the test says.

NEVER_DEAD = Timing(hello_timeout_ms=300, ping_interval_ms=100, dead_after_ms=60_000)
"""A raw device never answers pings."""


async def welcomed(hub: Hub, token: str | None = None) -> tuple[RawDevice, Welcome]:
    device = await RawDevice.connect(hub)
    device.send(Hello(token, 5000, echo_manifest()))
    welcome = await device.receive()
    assert isinstance(welcome, Welcome)
    return device, welcome


async def command(device: RawDevice) -> Command:
    message = await device.receive()
    assert isinstance(message, Command)
    return message


async def test_the_hub_sends_the_stored_volume_first_after_every_welcome(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD)
    async with OwnerClient(hub.owner_socket) as owner:
        first, welcome = await welcomed(hub)
        status = await command(first)
        assert status.name == "status"  # no volume stored: the device keeps its default
        first.send(Reply(status.id, {}))
        volume = asyncio.create_task(owner.set_volume(welcome.name, 60))
        set_volume = await command(first)
        first.send(Reply(set_volume.id, {}))
        assert (await volume)["applied"]
        first.close()

        again, _ = await welcomed(hub, welcome.token)
        commands = [await command(again), await command(again)]

    assert [(c.cap, c.name, c.args) for c in commands] == [
        ("speaker", "set_volume", {"level": 60}),
        (None, "status", {"state": "no_agent"}),
    ]
