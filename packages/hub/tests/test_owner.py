"""The owner socket: who may use it and how it refuses."""

import asyncio
import json
import os
import socket
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from tinyaisense_hub import Hub, HubError, OwnerClient
from tinyaisense_sim import Simulator

StartHub = Callable[..., Awaitable[Hub]]


async def test_the_owner_socket_is_for_the_owner_only(hub: Hub) -> None:
    assert stat.S_IMODE(os.stat(hub.owner_socket).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(hub.owner_socket.parent).st_mode) == 0o700


async def test_an_unknown_device_is_not_found(owner: OwnerClient) -> None:
    with pytest.raises(HubError) as refused:
        await owner.approve("02aa")

    assert refused.value.code == "not_found"


async def test_a_prefix_that_matches_several_devices_is_ambiguous(
    sim: Simulator, owner: OwnerClient
) -> None:
    await sim.add("m5stack-atom-echo", "kitchen")
    await sim.add("m5stack-atom-echo", "den")

    with pytest.raises(HubError) as refused:
        await owner.approve("02")

    assert refused.value.code == "ambiguous"


async def test_bad_requests_get_json_rpc_errors_with_a_code(hub: Hub) -> None:
    reader, writer = await asyncio.open_unix_connection(hub.owner_socket)
    requests = [
        b"not json",
        b'{"jsonrpc":"2.0","id":1,"method":"reboot_everything"}',
        b'{"jsonrpc":"2.0","id":2,"method":"approve","params":{"device":7}}',
        b'{"jsonrpc":"2.0","id":3,"method":"devices","params":{"verbose":true}}',
    ]
    codes: list[tuple[int, str]] = []
    for request in requests:
        writer.write(request + b"\n")
        error = json.loads(await reader.readline())["error"]
        codes.append((error["code"], error["data"]["code"]))
    writer.close()

    assert codes == [
        (-32700, "bad_request"),
        (-32601, "unknown_method"),
        (-32602, "invalid_params"),
        (-32602, "invalid_params"),
    ]


async def test_a_second_hub_on_the_same_runtime_dir_refuses_to_start(
    hub: Hub, start_hub: StartHub
) -> None:
    with pytest.raises(HubError) as refused:
        await start_hub()

    assert refused.value.code == "already_running"


async def test_a_hub_replaces_the_socket_a_dead_hub_left_behind(
    start_hub: StartHub, runtime_dir: Path
) -> None:
    stale = runtime_dir / "owner.sock"
    with socket.socket(socket.AF_UNIX) as dead:
        dead.bind(str(stale))

    hub = await start_hub()

    async with OwnerClient(hub.owner_socket) as owner:
        assert await owner.devices() == []


async def test_an_approve_the_hub_cannot_save_fails_and_can_be_retried(
    sim: Simulator, owner: OwnerClient, tmp_path: Path
) -> None:
    device = await sim.add("m5stack-atom-echo", "kitchen")
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o500)

    with pytest.raises(HubError) as refused:
        await owner.approve(device.device_id, "kitchen")

    assert refused.value.code == "failed"
    assert [(d["name"], d["pending"]) for d in await owner.devices()] == [(None, True)]
    state_dir.chmod(0o700)
    await owner.approve(device.device_id, "kitchen")
    await device.wait_for(lambda: device.state == "online")


async def test_the_hub_locks_down_a_runtime_dir_others_could_reach(
    start_hub: StartHub, runtime_dir: Path
) -> None:
    os.chmod(runtime_dir, 0o777)

    hub = await start_hub()

    assert stat.S_IMODE(os.stat(hub.owner_socket.parent).st_mode) == 0o700
