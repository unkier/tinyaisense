"""Generic commands, the agent LED claim and the Status layering, through the agent API."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
import tinyaisense
from tinyaisense_hub import Hub, OwnerClient, Timing
from tinyaisense_sim import Light, SimDevice, Simulator
from tinyaisense_wire import Command, Hello, JsonObject, Reply, Welcome

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


async def test_an_agent_sets_the_led_with_the_generic_command(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        result = await agent.command("kitchen/led", "set", {"color": "#FF0000"})

        assert result == {}
        assert kitchen.status == "claim"
        # The Echo caps brightness 100 at 64 of 255.
        assert kitchen.lights() == (Light("#ff0000", 64, "solid", 1000),)


async def test_the_led_shows_no_agent_without_a_session_and_agent_while_one_exists(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    assert kitchen.status == "no_agent"

    async with tinyaisense.connect(hub.agent_url, TOKEN):
        await kitchen.wait_for(lambda: kitchen.status == "agent")
        assert kitchen.lights() == (Light("#000000", 0),)  # `agent` is off

    await kitchen.wait_for(lambda: kitchen.status == "no_agent")


async def test_a_device_that_comes_online_during_a_session_shows_agent(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        kitchen = await paired(sim, owner)

        assert (await anext(notifications)).method == "device.online"
        assert kitchen.status == "agent"


async def test_led_release_hands_the_led_back_to_the_status(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await agent.command("kitchen/led", "set", {"color": "#00ff00"})

        await agent.led_release("kitchen")

        assert kitchen.status == "agent"
        assert kitchen.lights() == (Light("#000000", 0),)


async def test_releasing_an_unclaimed_led_does_nothing(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await agent.led_release("kitchen")

        assert kitchen.status == "agent"


async def test_led_release_of_an_unknown_device_is_unknown_address(hub: Hub) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.led_release("attic")

    assert refused.value.code == "unknown_address"


async def test_ending_the_session_releases_its_led_claims(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await agent.command("kitchen/led", "off")
        assert kitchen.status == "claim"

    await kitchen.wait_for(lambda: kitchen.status == "no_agent")


async def test_the_agents_led_claim_survives_a_device_reconnect(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        await agent.command("kitchen/led", "set", {"color": "#0000ff", "pattern": "pulse"})

        kitchen.faults.rst()

        assert (await anext(notifications)).method == "device.offline"
        assert (await anext(notifications)).method == "device.online"
        # By then the hub has brought the device up to date.
        assert kitchen.status == "claim"
        assert kitchen.lights() == (Light("#0000ff", 64, "pulse", 1000),)


async def test_a_released_claim_is_not_reapplied_after_a_reconnect(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        await agent.command("kitchen/led", "set", {"color": "#0000ff"})
        await agent.led_release("kitchen")

        kitchen.faults.rst()

        assert (await anext(notifications)).method == "device.offline"
        assert (await anext(notifications)).method == "device.online"
        assert kitchen.status == "agent"


async def test_a_led_command_the_device_failed_is_not_reapplied_after_a_reconnect(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        await agent.command("kitchen/led", "set", {"color": "#ff0000"})
        kitchen.faults.fail_next("busy")
        with pytest.raises(tinyaisense.ApiError):
            await agent.command("kitchen/led", "set", {"color": "#0000ff"})

        kitchen.faults.rst()

        assert (await anext(notifications)).method == "device.offline"
        assert (await anext(notifications)).method == "device.online"
        assert kitchen.lights() == (Light("#ff0000", 64),)


async def test_a_session_that_ends_during_its_first_led_command_releases_the_led(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        kitchen.faults.delay_replies_ms = 300
        pending = asyncio.create_task(agent.command("kitchen/led", "off"))
        await kitchen.wait_for(lambda: kitchen.status == "claim")  # its reply is held
        pending.cancel()

    await kitchen.wait_for(lambda: kitchen.status == "no_agent")


async def test_a_takeover_releases_the_old_sessions_led_claim(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as old:
        await old.command("kitchen/led", "set", {"color": "#ff0000"})

        async with tinyaisense.connect(hub.agent_url, TOKEN, takeover=True) as new:
            await new.devices()  # its Session has started: the old one has ended

            await kitchen.wait_for(lambda: kitchen.status == "agent")
            assert old.ended == "taken_over"


# Addresses and errors.


async def test_a_bare_capability_id_works_when_one_device_has_it(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await agent.command("led", "set", {"color": "#ffffff", "brightness": 50})

        assert kitchen.lights() == (Light("#ffffff", 32),)


async def test_an_ambiguous_bare_id_is_invalid_and_lists_the_candidates(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    await paired(sim, owner, "kitchen")
    await paired(sim, owner, "den")
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.command("led", "off")

    assert refused.value.code == "invalid_args"
    assert refused.value.data["candidates"] == ["den/led", "kitchen/led"]
    assert "den/led, kitchen/led" in refused.value.message


@pytest.mark.parametrize("address", ["attic/led", "kitchen/lamp", "lamp", "kitchen"])
async def test_an_address_nothing_has_is_unknown(
    hub: Hub, sim: Simulator, owner: OwnerClient, address: str
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.command(address, "off")

    assert refused.value.code == "unknown_address"


@pytest.mark.parametrize(
    ("address", "name", "args"),
    [
        ("kitchen/led", "set", {"color": "red"}),
        ("kitchen/led", "set", {"color": "#ffffff", "brightness": 101}),
        ("kitchen/led", "set", {}),
    ],
)
async def test_args_that_break_the_kind_contract_are_invalid(
    hub: Hub, sim: Simulator, owner: OwnerClient, address: str, name: str, args: JsonObject
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.command(address, name, args)

        assert refused.value.code == "invalid_args"
        assert kitchen.status != "claim"


@pytest.mark.parametrize(
    ("address", "name", "args"),
    [
        ("kitchen/speaker", "set_volume", {"level": 50}),
        ("kitchen/button", "set_reflex", {"armed": False}),
        ("kitchen/led", "release", {}),
        ("kitchen/led", "explode", {}),
    ],
)
async def test_hub_only_and_unknown_commands_are_unsupported(
    hub: Hub, sim: Simulator, owner: OwnerClient, address: str, name: str, args: JsonObject
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.command(address, name, args)

    assert refused.value.code == "unsupported"


async def test_the_devices_own_errors_pass_through(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.command("kitchen/led", "set", {"color": "#ffffff", "index": 1})

    assert refused.value.code == "invalid_args"  # the Echo has one LED: index 0
    assert "index" in refused.value.message


async def test_a_command_to_an_offline_device_is_disconnected(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        await kitchen.close()
        assert (await anext(notifications)).method == "device.offline"

        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.command("kitchen/led", "off")

    assert refused.value.code == "disconnected"


# Timeouts and faults.


async def test_delay_replies_fault_a_reply_past_the_timeout_fails_the_command_with_timeout(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        kitchen.faults.delay_replies_ms = 1300  # the led timeout is 1000

        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.command("kitchen/led", "set", {"color": "#ff0000"})

        assert refused.value.code == "timeout"
        assert kitchen.status == "claim"  # the device did it, it just replied late
        # Sent once the first timed out, so its reply comes after the late one, which the hub
        # drops.
        kitchen.faults.delay_replies_ms = 600
        assert await agent.command("kitchen/led", "set", {"color": "#00ff00"}) == {}
        assert kitchen.reconnects == 0


async def test_fail_next_fault_the_devices_error_reaches_the_agent(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        kitchen.faults.fail_next("busy")

        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.command("kitchen/led", "off")

        assert refused.value.code == "busy"
        assert kitchen.status == "agent"  # it didn't do it
        assert await agent.command("kitchen/led", "off") == {}  # only the next one failed


async def test_a_command_in_flight_when_the_device_drops_fails_disconnected(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        kitchen.faults.delay_replies_ms = 60_000
        pending = asyncio.create_task(agent.command("kitchen/led", "off"))
        await kitchen.wait_for(lambda: kitchen.status == "claim")

        kitchen.faults.rst()

        with pytest.raises(tinyaisense.ApiError) as refused:
            await pending
    assert refused.value.code == "disconnected"


async def test_a_device_that_is_slow_to_reply_holds_up_no_other_device(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner, "kitchen")
    await paired(sim, owner, "den")
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        kitchen.faults.delay_replies_ms = 60_000
        slow = asyncio.create_task(agent.command("kitchen/led", "off"))
        await kitchen.wait_for(lambda: kitchen.status == "claim")

        assert await agent.command("den/led", "off") == {}
        assert not slow.done()
        with pytest.raises(tinyaisense.ApiError):
            await slow


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


async def test_device_online_waits_for_the_replies_to_the_bring_up(start_hub: StartHub) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        device, _ = await welcomed(hub)
        status = await command(device)
        assert (status.cap, status.name, status.args) == (None, "status", {"state": "agent"})

        device.close()  # before it replies

        assert (await anext(notifications)).method == "device.offline"


async def test_the_status_and_the_led_claim_go_out_pipelined_after_welcome(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        first, welcome = await welcomed(hub)
        status = await command(first)
        first.send(Reply(status.id, {}))
        assert (await anext(notifications)).method == "device.online"
        name = welcome.name
        led = asyncio.create_task(agent.command(f"{name}/led", "set", {"color": "#123456"}))
        set_led = await command(first)
        first.send(Reply(set_led.id, {}))
        await led
        first.close()
        assert (await anext(notifications)).method == "device.offline"

        again, _ = await welcomed(hub, welcome.token)
        # Both go out before either reply is in.
        status, set_led = await command(again), await command(again)
        again.send(Reply(set_led.id, {}))  # out of order across capabilities
        again.send(Reply(status.id, {}))

        online = await anext(notifications)
        assert online.method == "device.online"
        assert [(c.id, c.cap, c.name, c.args) for c in (status, set_led)] == [
            (1, None, "status", {"state": "agent"}),
            (2, "led", "set", {"color": "#123456"}),
        ]


async def test_an_op_on_a_device_waits_for_the_replies_to_the_one_before(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        device, welcome = await welcomed(hub)
        status = await command(device)
        device.send(Reply(status.id, {}))
        assert (await anext(notifications)).method == "device.online"
        led = f"{welcome.name}/led"

        first = asyncio.create_task(agent.command(led, "set", {"color": "#ff0000"}))
        second = asyncio.create_task(agent.command(led, "off"))
        set_led = await command(device)
        off = await command(device)  # only once `set` got its reply, or timed out
        device.send(Reply(set_led.id, {}))
        device.send(Reply(off.id, {}))

        # Had `off` gone out before `set` was over, `set` would have had its reply in time.
        with pytest.raises(tinyaisense.ApiError) as refused:
            await first
        assert refused.value.code == "timeout"
        assert off.name == "off"
        assert await second == {}
