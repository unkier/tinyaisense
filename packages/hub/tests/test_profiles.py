"""The hub with Boards other than the Echo: a full-duplex one, and one with a capability of a
Kind the hub doesn't know."""

import logging

import pytest
import tinyaisense
from tinyaisense_hub import Hub, OwnerClient
from tinyaisense_sim import Board, Light, SimDevice, Simulator, from_manifest, sources

from .test_faults import TOKEN, next_of

THERMO = {
    "board": "thermo-1",
    "capabilities": [
        {"id": "temp", "kind": "thermometer", "params": {"unit": "C"}},
        {"id": "led", "kind": "led", "params": {"count": 1}},
        {"id": "button", "kind": "button"},
    ],
    "exclusive": [],
}
"""A Board with a capability of a Kind no hub knows yet."""


async def paired(sim: Simulator, owner: OwnerClient, board: str | Board) -> SimDevice:
    device = await sim.add(board, "kitchen")
    await owner.approve(device.device_id, "kitchen")
    await device.wait_for(lambda: device.state == "online")
    return device


async def test_a_duplex_device_plays_and_streams_its_mic_at_once(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner, "sim-duplex")
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        speaker = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(speaker, sources.tone(300))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 5)

        mic = await agent.stream_start("kitchen/mic")
        drained = await agent.stream_drain(speaker)
        stopped = await agent.stream_stop(mic)

    assert (drained["reason"], drained["stats"]["played_ms"]) == ("drained", 300)
    assert stopped["reason"] == "stopped"


async def test_a_capability_of_an_unknown_kind_is_ignored_with_a_warning_and_the_rest_works(
    hub: Hub, sim: Simulator, owner: OwnerClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    kitchen = await paired(sim, owner, from_manifest(THERMO))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()

        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.command("kitchen/temp", "read")
        await agent.command("kitchen/led", "set", {"color": "#00ff00"})
        assert kitchen.lights() == (Light("#00ff00", 255),)
        kitchen.button.press()
        down = await next_of(notifications, "button.down")

    assert refused.value.code == "unsupported"
    assert down.params["address"] == "kitchen/button"
    ignored = [r.getMessage() for r in caplog.records if "ignoring capability" in r.getMessage()]
    assert ignored == [
        f"device {kitchen.device_id}: ignoring capability 'temp' of unknown kind 'thermometer'"
    ]
