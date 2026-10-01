"""The hub under the Simulator's device faults: clock offset and drift, WiFi drops, reboots and
garbage on the Connection. The other faults' tests sit with what they disturb: the TCP ones in
test_connection.py and test_commands.py, the UDP ones in test_speaker_streams.py,
test_mic_streams.py and test_button_events.py."""

import asyncio
import time
from collections.abc import AsyncIterator

import tinyaisense
from tinyaisense import Notification
from tinyaisense_hub import Hub, OwnerClient
from tinyaisense_sim import Light, SimDevice, Simulator, sources

ECHO = "m5stack-atom-echo"
TOKEN = "agent-token"
"""The `start_hub` fixture's agent token."""


async def paired(sim: Simulator, owner: OwnerClient, name: str = "kitchen") -> SimDevice:
    device = await sim.add(ECHO, name)
    await owner.approve(device.device_id, name)
    await device.wait_for(lambda: device.state == "online")
    return device


async def next_of(notifications: AsyncIterator[Notification], *methods: str) -> Notification:
    """The next notification of one of `methods`, past the others."""
    async with asyncio.timeout(5):
        async for notification in notifications:
            if notification.method in methods:
                return notification
    raise AssertionError("the Session ended")


async def test_reboot_fault_the_device_comes_back_afresh_and_the_hub_reapplies_its_settings(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    await owner.set_volume("kitchen", 30)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        await agent.command("kitchen/led", "set", {"color": "#ff0000"})

        kitchen.faults.reboot(down_ms=300, reason="panic")

        await next_of(notifications, "device.offline")
        await next_of(notifications, "device.online")
        await kitchen.wait_for(lambda: kitchen.lights() == (Light("#ff0000", 64),))
        assert kitchen.speaker.level == 30
        diag = await owner.diag("kitchen")
        assert (diag["reset_reason"], diag["reconnects"]) == ("panic", 0)


async def test_clock_offset_fault_events_are_still_stamped_on_the_hubs_clock(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.clock_offset_us = 86_400_000_000  # up a day, by its clock
    kitchen.faults.reboot(down_ms=0)
    await kitchen.wait_for(lambda: kitchen.reconnects == 1 and kitchen.state == "online")
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        await asyncio.sleep(0.3)  # for the device clock's offset, from a few pongs

        pressed = time.time() * 1000
        kitchen.button.press()
        down = await next_of(notifications, "button.down")

    assert abs(down.t - pressed) < 150


async def test_drift_ppm_fault_the_hub_doesnt_make_up_for_a_fast_crystal_so_its_speaker_underruns(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.drift_ppm = 200_000  # it plays 20% fast: 1 s of audio in 833 ms
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        await asyncio.sleep(0.3)  # for the device clock's offset, from a few pongs
        pressed = time.time() * 1000
        kitchen.button.press()
        down = await next_of(notifications, "button.down")
        kitchen.button.release()
        await next_of(notifications, "utterance.end")  # the push to talk, with nothing heard

        handle = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(handle, sources.tone(1000))
        ended = await agent.stream_drain(handle)

    assert abs(down.t - pressed) < 150  # the clock sync keeps up with the drift
    # The hub's 60 ms lead runs out after 300 ms of it.
    assert ended["stats"]["underruns"] >= 1
    assert ended["stats"]["played_ms"] == 1000


async def test_wifi_drop_fault_the_hub_notices_the_silence_and_takes_the_device_back_after(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        await agent.command("kitchen/led", "set", {"color": "#ff0000"})

        kitchen.faults.wifi_drop(1500)  # past the hub's 800 ms dead timeout

        assert kitchen.status == "offline"
        await next_of(notifications, "device.offline")
        assert kitchen.faults.wifi_down  # the hub noticed by itself
        await next_of(notifications, "device.online")
        await kitchen.wait_for(lambda: kitchen.lights() == (Light("#ff0000", 64),))


async def test_send_garbage_fault_the_hub_closes_the_connection_and_the_device_comes_back(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()

        kitchen.faults.send_garbage()

        await next_of(notifications, "device.offline")
        await next_of(notifications, "device.online")
    assert kitchen.reconnects == 1
