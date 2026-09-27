"""Pairing, driven through a simulated device and the owner socket."""

import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import ANY

import pytest
from tinyaisense_hub import Hub, HubError, OwnerClient
from tinyaisense_sim import Simulator
from tinyaisense_sim.pytest_plugin import SimulatorFactory

ECHO = "m5stack-atom-echo"

StartHub = Callable[..., Awaitable[Hub]]
"""The `start_hub` fixture."""


async def test_an_unknown_device_shows_up_pending(sim: Simulator, owner: OwnerClient) -> None:
    device = await sim.add(ECHO, "kitchen")

    assert device.state == "pending"
    assert await owner.devices() == [
        {
            "device_id": device.device_id,
            "name": None,
            "pending": True,
            "online": True,
            "last_seen": ANY,
            "board": ECHO,
            "firmware": device.manifest.firmware,
        }
    ]


async def test_approving_a_pending_device_welcomes_it_with_its_name_and_token(
    sim: Simulator, owner: OwnerClient
) -> None:
    device = await sim.add(ECHO, "kitchen")

    approved = await owner.approve(device.device_id[:6], "kitchen")

    assert approved == {"device_id": device.device_id, "name": "kitchen"}
    await device.wait_for(lambda: device.state == "online")
    assert device.name == "kitchen"
    assert device.token is not None
    assert [(d["name"], d["pending"]) for d in await owner.devices()] == [("kitchen", False)]


async def test_approving_without_a_name_uses_the_default_name(
    sim: Simulator, owner: OwnerClient
) -> None:
    device = await sim.add(ECHO, "kitchen")  # device_id 023171d89ad0

    assert await owner.approve(device.device_id) == {
        "device_id": "023171d89ad0",
        "name": "echo-9ad0",
    }


async def test_auto_approve_welcomes_a_new_device_at_once(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    hub = await start_hub(auto_approve=True)

    device = await simulator(hub.device_address).add(ECHO, "kitchen")

    assert (device.state, device.name) == ("online", "echo-9ad0")
    assert device.token is not None


async def test_a_paired_device_goes_straight_to_welcome_on_its_next_start(
    sim: Simulator, owner: OwnerClient
) -> None:
    device = await sim.add(ECHO, "kitchen")
    await owner.approve(device.device_id, "kitchen")
    await device.wait_for(lambda: device.state == "online")
    await device.close()

    again = await sim.add(ECHO, "kitchen")

    assert (again.state, again.name) == ("online", "kitchen")


async def test_a_simulator_state_dir_keeps_tokens_across_runs(
    hub: Hub, owner: OwnerClient, simulator: SimulatorFactory, tmp_path: Path
) -> None:
    first = simulator(hub.device_address, tmp_path / "sim")
    device = await first.add(ECHO, "kitchen")
    await owner.approve(device.device_id, "kitchen")
    await device.wait_for(lambda: device.state == "online")
    await first.close()

    again = await simulator(hub.device_address, tmp_path / "sim").add(ECHO, "kitchen")

    assert (again.state, again.name) == ("online", "kitchen")


async def test_without_a_state_dir_tokens_die_with_the_simulator(
    hub: Hub, owner: OwnerClient, simulator: SimulatorFactory
) -> None:
    first = simulator(hub.device_address)
    device = await first.add(ECHO, "kitchen")
    await owner.approve(device.device_id, "kitchen")
    await device.wait_for(lambda: device.state == "online")
    await first.close()

    again = await simulator(hub.device_address).add(ECHO, "kitchen")

    assert again.state == "pending"


async def test_names_must_match_the_name_pattern(sim: Simulator, owner: OwnerClient) -> None:
    device = await sim.add(ECHO, "kitchen")

    with pytest.raises(HubError) as refused:
        await owner.approve(device.device_id, "Kitchen 2")

    assert refused.value.code == "invalid_name"


async def test_names_are_unique(sim: Simulator, owner: OwnerClient) -> None:
    kitchen = await sim.add(ECHO, "kitchen")
    den = await sim.add(ECHO, "den")
    await owner.approve(kitchen.device_id, "kitchen")

    with pytest.raises(HubError) as refused:
        await owner.approve(den.device_id, "kitchen")

    assert refused.value.code == "name_taken"


async def test_rename_drops_the_connection_and_the_device_returns_under_the_new_name(
    sim: Simulator, owner: OwnerClient
) -> None:
    device = await sim.add(ECHO, "kitchen")
    await owner.approve(device.device_id, "kitchen")
    await device.wait_for(lambda: device.state == "online")

    assert await owner.rename("kitchen", "den") == {"device_id": device.device_id, "name": "den"}

    await device.wait_for(lambda: device.name == "den")
    assert device.state == "online"
    assert [d["name"] for d in await owner.devices()] == ["den"]


async def test_a_forgotten_device_must_pair_again(sim: Simulator, owner: OwnerClient) -> None:
    device = await sim.add(ECHO, "kitchen")
    await owner.approve(device.device_id, "kitchen")
    await device.wait_for(lambda: device.state == "online")

    await owner.forget("kitchen")

    await device.wait_for(lambda: device.state == "pending")
    assert device.last_error == "unauthorized"
    assert device.token is None
    assert [(d["name"], d["pending"]) for d in await owner.devices()] == [(None, True)]


async def test_paired_devices_survive_a_hub_restart(
    start_hub: StartHub, simulator: SimulatorFactory, tmp_path: Path
) -> None:
    hub = await start_hub()
    device = await simulator(hub.device_address, tmp_path / "sim").add(ECHO, "kitchen")
    async with OwnerClient(hub.owner_socket) as owner:
        await owner.approve(device.device_id, "kitchen")
    await device.wait_for(lambda: device.state == "online")
    await hub.close()

    hub = await start_hub()

    again = await simulator(hub.device_address, tmp_path / "sim").add(ECHO, "kitchen")
    assert (again.state, again.name) == ("online", "kitchen")
    async with OwnerClient(hub.owner_socket) as owner:
        assert [(d["name"], d["pending"]) for d in await owner.devices()] == [("kitchen", False)]


async def test_the_hub_stores_only_a_hash_of_the_token(
    hub: Hub, sim: Simulator, owner: OwnerClient, tmp_path: Path
) -> None:
    device = await sim.add(ECHO, "kitchen")
    await owner.approve(device.device_id, "kitchen")
    await device.wait_for(lambda: device.state == "online")
    assert device.token is not None
    await hub.close()  # lets the pending saves land

    stored = "".join(p.read_text() for p in (tmp_path / "state").rglob("*") if p.is_file())

    assert device.token not in stored
    assert hashlib.sha256(device.token.encode()).hexdigest() in stored
