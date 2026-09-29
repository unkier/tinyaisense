"""The Reflex: the hub arms it on every device while a Session exists, and turns what it does
into barge-in and push-to-talk: docs/protocol/agent.md, docs/protocol/kinds/button.md."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
import tinyaisense
from tinyaisense_hub import Hub, OwnerClient, Timing
from tinyaisense_hub.speech import fake
from tinyaisense_sim import SimDevice, Simulator, sources
from tinyaisense_sim.pytest_plugin import SimulatorFactory
from tinyaisense_wire import Command, Event, Reply

from .conftest import FAST_TIMING, StartHub
from .raw_device import RawDevice
from .test_commands import NEVER_DEAD, TOKEN, welcomed
from .test_listen import SPEECH, paired, summary, utterance

FORMAT = {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": 10}
ARM = (
    "button",
    "set_reflex",
    {
        "armed": True,
        "stop": ["speaker"],
        "start": {"cap": "mic", "format": FORMAT},
    },
)
DISARM = ("button", "set_reflex", {"armed": False})
AGENT = (None, "status", {"state": "agent"})
NO_AGENT = (None, "status", {"state": "no_agent"})
PHRASE = "Turn on the kitchen lights, please."
"""350 ms of speech, as the fake TTS speaks it."""


# Arming, seen by a hand-rolled device.


async def commands(device: RawDevice, count: int) -> list[Command]:
    """The next `count` commands, each replied to `{}` once they're all in."""
    received: list[Command] = []
    for _ in range(count):
        message = await device.receive()
        assert isinstance(message, Command)
        received.append(message)
    for command in received:
        device.send(Reply(command.id, {}))
    return received


def spec(command: Command) -> tuple[str | None, str, object]:
    return command.cap, command.name, command.args


async def test_a_device_that_comes_online_during_a_session_is_armed_after_its_status(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD)
    async with tinyaisense.connect(hub.agent_url, TOKEN):
        device, _ = await welcomed(hub)

        assert [spec(c) for c in await commands(device, 2)] == [AGENT, ARM]


async def test_a_session_arms_every_device_and_its_end_disarms_them_before_no_agent(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD)
    device, _ = await welcomed(hub)
    assert [spec(c) for c in await commands(device, 1)] == [NO_AGENT]

    async with tinyaisense.connect(hub.agent_url, TOKEN):
        assert [spec(c) for c in await commands(device, 2)] == [AGENT, ARM]

    assert [spec(c) for c in await commands(device, 2)] == [DISARM, NO_AGENT]


async def test_reflex_set_toggles_armed_and_the_choice_outlives_a_reconnect(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        device, welcome = await welcomed(hub)
        await commands(device, 2)
        assert (await anext(notifications)).method == "device.online"

        disarm = asyncio.create_task(agent.reflex_set(welcome.name, armed=False))
        assert [spec(c) for c in await commands(device, 1)] == [DISARM]
        await disarm
        device.close()
        assert (await anext(notifications)).method == "device.offline"

        again, _ = await welcomed(hub, welcome.token)
        assert [spec(c) for c in await commands(again, 1)] == [AGENT]
        assert (await anext(notifications)).method == "device.online"

        arm = asyncio.create_task(agent.reflex_set(welcome.name, armed=True))
        assert [spec(c) for c in await commands(again, 1)] == [ARM]
        await arm


async def test_ending_a_session_stops_its_streams_releases_the_leds_disarms_then_no_agent(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD)
    device, welcome = await welcomed(hub)
    await commands(device, 1)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await commands(device, 2)
        led = asyncio.create_task(agent.command(f"{welcome.name}/led", "set", {"color": "#123456"}))
        await commands(device, 1)
        await led
        stream = asyncio.create_task(agent.stream_start(f"{welcome.name}/speaker"))
        start = await device.receive()
        assert isinstance(start, Command)
        device.send(Reply(start.id, {"format": FORMAT}))
        await stream

    assert [spec(c)[:2] for c in await commands(device, 4)] == [
        ("speaker", "stop"),
        ("led", "release"),
        DISARM[:2],
        NO_AGENT[:2],
    ]


async def test_the_hub_refuses_a_speaker_start_while_the_human_holds_the_button(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        device, welcome = await welcomed(hub)
        await commands(device, 2)
        device.send(Event(0, "button", "down", 0, {}))
        device.send(Event(1, "mic", "started", 0, {"stream": 128, "format": FORMAT}))
        await methods(notifications, "utterance.start")

        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.stream_start(f"{welcome.name}/speaker")
        await asyncio.sleep(0.3)  # no click
        device.send(Event(2, "button", "up", 0, {}))
        stopped = {"stream": 128, "reason": "reflex", "last_seq": None, "dropped": 0}
        device.send(Event(3, "mic", "stopped", 0, stopped))
        await methods(notifications, "utterance.end")
        led = asyncio.create_task(agent.command(f"{welcome.name}/led", "off"))
        # The device closed the Reflex's stream itself: the hub sends it no `stop`.
        assert [spec(c) for c in await commands(device, 2)] == [
            (None, "status", {"state": "thinking"}),
            ("led", "off", {}),
        ]
        await led

    assert refused.value.code == "busy"


# Push-to-talk and barge-in, through the agent API, with the fake speech plugin.


@pytest.fixture
async def hub(start_hub: StartHub, request: pytest.FixtureRequest) -> Hub:
    """With `SPEECH`; parametrize it indirectly with a `Timing` for another."""
    timing: Timing = getattr(request, "param", FAST_TIMING)
    return await start_hub(speech=SPEECH, timing=timing)


async def heard(agent: tinyaisense.Session) -> list[tinyaisense.Notification]:
    """The next Utterance's notifications, up to its `utterance.end`."""
    async with asyncio.timeout(10):
        return await utterance(agent)


async def armed(device: SimDevice) -> None:
    """Wait until the hub has armed `device`'s Reflex."""
    async with asyncio.timeout(5):
        await device.wait_for(lambda: device.armed)


async def test_push_to_talk_is_an_utterance_that_the_release_ends_not_the_turn(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await armed(kitchen)
        # A complete turn, then more silence than ends one: only the release ends it.
        await kitchen.talk(fake.encode(PHRASE) + sources.silence(1500))
        said = await heard(agent)

    assert summary(said) == [
        ("utterance.start", "button"),
        ("transcript.partial", PHRASE),
        ("utterance.end", "button"),
    ]
    assert said[0].params["address"] == "kitchen/mic"
    assert said[-1].params["transcript"] == PHRASE
    assert 1600 <= said[-1].params["stats"]["duration_ms"] <= 2200


async def methods(notifications: AsyncIterator[tinyaisense.Notification], until: str) -> list[str]:
    """The notifications' methods, up to `until`'s, past `transcript.partial`s."""
    seen: list[str] = []
    async with asyncio.timeout(10):
        async for notification in notifications:
            if notification.method != "transcript.partial":
                seen.append(notification.method)
            if notification.method == until:
                return seen
    raise AssertionError("the Session ended")


async def test_a_press_cuts_the_say_before_the_utterance_starts(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    first = "One two three four, five six seven eight nine ten eleven twelve. Thirteen."
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await armed(kitchen)
        notifications = agent.notifications()
        cut = asyncio.ensure_future(agent.say("kitchen/speaker", first))
        queued = asyncio.ensure_future(agent.say("kitchen/speaker", "Never heard."))
        # Past the first clause, 190 ms, well into the second, which ends at 630 ms.
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 35)
        kitchen.button.press()
        order = await methods(notifications, "utterance.start")
        kitchen.button.release()
        cut_result, queued_result = await cut, await queued

    assert order == ["button.down", "say.ended", "say.ended", "utterance.start"]
    assert cut_result | {"played_ms": 0} == {
        "reason": "reflex",
        "played_ms": 0,
        "interrupted": True,
        "heard_text": "One two three four,",
    }
    assert 190 < cut_result["played_ms"] < 630
    assert queued_result == {
        "reason": "reflex",
        "played_ms": 0,
        "interrupted": True,
        "heard_text": "",
    }


async def test_a_click_while_armed_ends_the_utterance_click_with_nothing_heard(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await armed(kitchen)
        notifications = agent.notifications()
        await kitchen.button.click()
        order = await methods(notifications, "button.click")
        said = await heard(agent)

    assert order == ["button.down", "utterance.start", "button.up", "button.click"]
    assert said[-1].params | {"stats": {}, "t": 0} == {
        "utterance_id": said[-1].params["utterance_id"],
        "reason": "click",
        "transcript": "",
        "stats": {},
        "t": 0,
    }


async def test_a_press_held_while_armed_is_no_long_press(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await armed(kitchen)
        notifications = agent.notifications()
        kitchen.button.press()
        await asyncio.sleep(1.2)
        kitchen.button.release()
        order = await methods(notifications, "utterance.end")

    assert order == ["button.down", "utterance.start", "button.up", "utterance.end"]


async def test_a_press_during_a_listen_hands_it_to_the_human_with_what_was_said_before(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await armed(kitchen)
        notifications = agent.notifications()
        listening = asyncio.create_task(agent.listen("kitchen/mic"))
        await kitchen.wait_for(lambda: kitchen.mic.stream is not None)
        kitchen.mic.feed(fake.encode("So I was thinking that we could"))
        await kitchen.wait_for(lambda: not kitchen.mic.unheard)
        # A complete turn, then more silence than ends one: only the release ends it.
        await kitchen.talk(
            fake.encode(" go to the beach, what do you think?") + sources.silence(1500)
        )
        result = await listening
        order = await methods(notifications, "utterance.end")

    assert order == ["utterance.start", "button.down", "button.up", "utterance.end"]
    assert (result["reason"], result["transcript"]) == (
        "button",
        "So I was thinking that we could go to the beach, what do you think?",
    )


async def test_the_human_wins_the_speaker_while_holding_the_button(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await armed(kitchen)
        notifications = agent.notifications()
        kitchen.button.press()
        await methods(notifications, "utterance.start")

        with pytest.raises(tinyaisense.ApiError) as stream:
            await agent.stream_start("kitchen/speaker")
        with pytest.raises(tinyaisense.ApiError) as say:
            await agent.say("kitchen/speaker", "Excuse me.")
        kitchen.button.release()
        await methods(notifications, "utterance.end")
        handle = await agent.stream_start("kitchen/speaker")

    assert (stream.value.code, say.value.code) == ("busy", "busy")
    assert handle


async def test_with_no_session_the_reflex_is_disarmed_and_a_press_opens_nothing(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    assert not kitchen.armed
    async with tinyaisense.connect(hub.agent_url, TOKEN):
        await armed(kitchen)
    async with asyncio.timeout(5):
        await kitchen.wait_for(lambda: not kitchen.armed)

    kitchen.button.press()

    assert kitchen.mic.stream is None


async def test_frames_that_beat_mic_started_are_kept(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await armed(kitchen)
        kitchen.faults.delay_events_ms = 150
        await kitchen.talk(fake.encode(PHRASE) + sources.silence(300))
        said = await heard(agent)

    assert said[-1].params["transcript"] == PHRASE
    assert said[-1].params["stats"]["duration_ms"] >= 650


async def test_push_to_talk_with_no_stt_is_an_utterance_with_no_transcript(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    hub = await start_hub()
    sim = simulator(hub.device_address)
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await armed(kitchen)
        await kitchen.talk(fake.encode(PHRASE))
        said = await heard(agent)

    assert summary(said) == [("utterance.start", "button"), ("utterance.end", "button")]
    assert "transcript" not in said[-1].params


async def test_a_raw_mic_stream_preempts_push_to_talk(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await armed(kitchen)
        notifications = agent.notifications()
        kitchen.button.press()
        await methods(notifications, "utterance.start")

        await agent.stream_start("kitchen/mic")
        said = await heard(agent)
        kitchen.button.release()

    assert said[-1].params["reason"] == "stopped"
    assert kitchen.mic.stream is None


@pytest.mark.parametrize("hub", [replace(FAST_TIMING, mic_tail_ms=1000)], indirect=True)
async def test_push_to_talk_while_the_last_mic_stream_waits_for_its_tail_is_heard(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_up.drop(1, range(1000))  # the raw stream's tail never comes
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await armed(kitchen)
        handle = await agent.stream_start("kitchen/mic")
        await asyncio.sleep(0.1)
        stop = asyncio.create_task(agent.stream_stop(handle))
        await kitchen.wait_for(lambda: kitchen.mic.stream is None)

        await kitchen.talk(fake.encode(PHRASE) + sources.silence(300))
        said = await heard(agent)
        await stop

    assert summary(said)[0] == ("utterance.start", "button")
    assert said[-1].params["transcript"] == PHRASE


async def test_a_session_that_ends_mid_push_to_talk_delivers_it_disconnected_first(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as old:
        await armed(kitchen)
        notifications = old.notifications()
        kitchen.button.press()
        await methods(notifications, "utterance.start")

        async with tinyaisense.connect(hub.agent_url, TOKEN, takeover=True):
            assert kitchen.mic.stream is None
            rest = [n.method async for n in notifications]
            kitchen.button.release()

    assert rest == ["utterance.end", "session.ended"]


async def test_a_device_that_drops_mid_push_to_talk_ends_it_disconnected(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await armed(kitchen)
        notifications = agent.notifications()
        kitchen.button.press()
        await methods(notifications, "utterance.start")

        kitchen.faults.rst()
        said = await heard(agent)

    assert said[-1].params["reason"] == "disconnected"


async def test_a_disarmed_device_has_no_push_to_talk_and_its_long_press_is_back(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await armed(kitchen)
        notifications = agent.notifications()
        await agent.reflex_set("kitchen", armed=False)
        assert not kitchen.armed

        kitchen.button.press()
        order = await methods(notifications, "button.long_press")
        kitchen.button.release()

    assert order == ["button.down", "button.long_press"]
    assert kitchen.mic.stream is None


async def test_a_reflex_acts_only_on_its_own_device(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    hall = await paired(sim, owner, "hall")
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await armed(kitchen)
        await armed(hall)
        await agent.stream_start("hall/speaker")
        notifications = agent.notifications()

        kitchen.button.press()
        await methods(notifications, "utterance.start")

        assert hall.speaker.stream is not None
        assert hall.mic.stream is None
        kitchen.button.release()


async def test_reflex_set_names_a_device(hub: Hub) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.reflex_set("nowhere", armed=False)

    assert refused.value.code == "unknown_address"
