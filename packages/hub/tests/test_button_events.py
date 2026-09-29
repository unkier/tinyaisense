"""Button Events and gestures, from a press on a device to the agent's socket:
docs/protocol/agent.md#notifications and docs/protocol/kinds/button.md."""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator

import tinyaisense
from tinyaisense import Notification
from tinyaisense_hub import Hub, OwnerClient
from tinyaisense_sim import SimDevice, Simulator

ECHO = "m5stack-atom-echo"
TOKEN = "agent-token"
"""The `start_hub` fixture's agent token."""


async def paired(sim: Simulator, owner: OwnerClient, name: str = "kitchen") -> SimDevice:
    device = await sim.add(ECHO, name)
    await owner.approve(device.device_id, name)
    await device.wait_for(lambda: device.state == "online")
    return device


async def next_button(notifications: AsyncIterator[Notification]) -> Notification:
    """The next button Event, past other notifications."""
    async with asyncio.timeout(2):
        async for notification in notifications:
            if notification.method.startswith("button."):
                return notification
    raise AssertionError("the Session ended")


async def test_the_agent_gets_a_press_as_button_down_then_button_up(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()

        kitchen.button.press()
        down = await next_button(notifications)
        kitchen.button.release()
        up = await next_button(notifications)

    assert (down.method, down.params["address"], down.params["data"]) == (
        "button.down",
        "kitchen/button",
        {},
    )
    assert (up.method, up.params["address"], up.params["data"]) == (
        "button.up",
        "kitchen/button",
        {},
    )


async def test_an_events_t_is_when_it_happened_on_the_device_not_when_it_came_in(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_up.loss = 1.0  # no mirrors: the Event comes by TCP only
    kitchen.faults.delay_events_ms = 300
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        await asyncio.sleep(0.3)  # for the device clock's offset, from a few pongs

        pressed = time.time() * 1000
        kitchen.button.press()
        down = await next_button(notifications)
        came_in = time.time() * 1000

    assert came_in - pressed >= 300
    assert abs(down.t - pressed) < 150  # not when it came in, 300 ms later


async def more_buttons(notifications: AsyncIterator[Notification], ms: int = 100) -> list[str]:
    """The methods of the button Events that come in the next `ms`. It ends the iterator."""
    methods: list[str] = []
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(ms / 1000):
            async for notification in notifications:
                if notification.method.startswith("button."):
                    methods.append(notification.method)
    return methods


async def button_events(notifications: AsyncIterator[Notification], count: int) -> list[str]:
    """The next `count` button Events' methods, past other notifications."""
    return [(await next_button(notifications)).method for _ in range(count)]


async def test_a_quick_press_is_also_a_click_as_it_ends(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()

        await kitchen.button.click()
        methods = await button_events(notifications, 3)

    assert methods == ["button.down", "button.up", "button.click"]


async def test_two_quick_clicks_are_also_a_double_click(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()

        await kitchen.button.double_click()
        methods = await button_events(notifications, 7)

    assert methods == ["button.down", "button.up", "button.click"] * 2 + ["button.double_click"]


async def test_a_press_held_for_1_s_is_a_long_press_while_it_is_held(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await agent.reflex_set("kitchen", armed=False)  # else it's push-to-talk
        notifications = agent.notifications()

        kitchen.button.press()
        down = await next_button(notifications)
        long_press = await next_button(notifications)
        kitchen.button.release()
        up = await next_button(notifications)
        after = await more_buttons(notifications)

    assert (down.method, long_press.method, up.method) == (
        "button.down",
        "button.long_press",
        "button.up",
    )
    assert long_press.params["address"] == "kitchen/button"
    assert 995 <= long_press.t - down.t <= 1005
    assert after == []


async def edges(notifications: AsyncIterator[Notification], count: int) -> list[str]:
    """The next `count` raw button edges' methods, past other notifications."""
    methods: list[str] = []
    while len(methods) < count:
        method = (await next_button(notifications)).method
        if method in ("button.down", "button.up"):
            methods.append(method)
    return methods


async def test_dedupe_under_udp_loss_and_duplicates(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_up.loss = 0.5
    kitchen.faults.udp_up.duplicate = 1.0
    kitchen.faults.udp_up.jitter_ms = 10
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()

        for _ in range(20):
            await kitchen.button.click(ms=20)
            await asyncio.sleep(0.02)
        methods = await edges(notifications, 40)
        methods += [
            m for m in await more_buttons(notifications) if m in ("button.down", "button.up")
        ]

    assert methods == ["button.down", "button.up"] * 20
    assert kitchen.faults.udp_up.dropped > 0


async def test_tcp_stalled_delivery_still_arrives_via_the_udp_mirror(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.delay_events_ms = 500
    loop = asyncio.get_running_loop()
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()

        pressed = loop.time()
        await kitchen.button.click()
        methods = await button_events(notifications, 3)
        came_in = loop.time()
        # The TCP copies come in 500 ms late, and are dropped.
        methods += await more_buttons(notifications, ms=700)

    assert came_in - pressed < 0.4  # not when the TCP copy did, 500 ms later
    assert methods == ["button.down", "button.up", "button.click"]


async def test_the_owners_stats_show_the_latency_from_press_to_agent_socket(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    none_yet = (await owner.stats())["button_latency"]
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        await asyncio.sleep(0.3)  # for the device clock's offset, from a few pongs

        for _ in range(5):
            await kitchen.button.click(ms=20)
            await asyncio.sleep(0.02)
        await edges(notifications, 10)
        async with asyncio.timeout(1):
            while (latency := (await owner.stats())["button_latency"])["count"] < 10:  # noqa: ASYNC110 - polls the owner socket
                await asyncio.sleep(0.01)

    assert none_yet["count"] == 0
    assert none_yet["p99_ms"] is None
    assert latency["count"] == 10
    assert sum(bucket["count"] for bucket in latency["buckets"]) == 10
    assert latency["buckets"][-1]["le_ms"] is None
    assert 0 <= latency["p50_ms"] <= latency["p99_ms"] <= latency["max_ms"] < 250
