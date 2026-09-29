"""Streaming text, `say.open/append/close`, with the fake speech plugin:
docs/protocol/agent.md#streaming-text."""

import asyncio

import pytest
import tinyaisense
from tinyaisense_hub import Hub, OwnerClient
from tinyaisense_hub.speech import fake
from tinyaisense_sim import Simulator
from tinyaisense_sim.pytest_plugin import SimulatorFactory
from tinyaisense_wire import Event, FailedReply, Reply

from .conftest import StartHub
from .test_say import LONG, REGISTERS, SPEECH, TOKEN, VoiceParams, gaps, paired
from .test_speaker_streams import NEVER_DEAD, command, online


@pytest.fixture
async def hub(start_hub: StartHub) -> Hub:
    return await start_hub(speech=SPEECH)


async def test_speech_starts_after_the_first_clause_before_the_text_is_closed(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.say_open("kitchen/speaker")
        for delta in ("One two", " three four", ",", " five"):
            await agent.say_append(handle, delta)
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 1)
        await agent.say_append(handle, " six seven eight.")
        result = await agent.say_close(handle)

    assert fake.decode(kitchen.speaker.pcm()).text == "One two three four,five six seven eight."
    assert result == {
        "reason": "drained",
        "played_ms": 400,
        "interrupted": False,
        "heard_text": LONG,
    }


async def test_a_tail_of_four_words_is_spoken_once_no_text_has_come_for_500_ms(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    loop = asyncio.get_running_loop()
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.say_open("kitchen/speaker")
        await agent.say_append(handle, "Hi there")
        await asyncio.sleep(0.7)
        assert kitchen.speaker.captured == []  # under four words: it waits for more text
        appended = loop.time()
        await agent.say_append(handle, " you all.")
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 1)
        assert loop.time() - appended >= 0.5
        await agent.say_append(handle, " Bye.")
        result = await agent.say_close(handle)

    assert fake.decode(kitchen.speaker.pcm()).text == "Hi there you all.Bye."
    assert result == {
        "reason": "drained",
        "played_ms": 210,
        "interrupted": False,
        "heard_text": "Hi there you all. Bye.",
    }


async def test_a_say_queued_behind_an_open_one_follows_it_once_its_closed_with_no_gap(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.say_open("kitchen/speaker")
        await agent.say_append(handle, "One two three four, five")
        queued = asyncio.ensure_future(agent.say("kitchen/speaker", "Bye."))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 5)
        await agent.say_append(handle, " six seven eight.")
        streamed = await agent.say_close(handle)
        bye = await queued

    assert fake.decode(kitchen.speaker.pcm()).text == "One two three four,five six seven eight.Bye."
    assert len({frame.stream for frame in kitchen.speaker.captured}) == 1
    assert gaps(kitchen) == []
    assert streamed["reason"] == "drained"
    assert bye == {"reason": "drained", "played_ms": 40, "interrupted": False, "heard_text": "Bye."}


async def test_a_replacing_say_open_cuts_in_before_any_text_has_come(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        saying = asyncio.ensure_future(agent.say("kitchen/speaker", LONG))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 30)
        handle = await agent.say_open("kitchen/speaker", replace=True)
        cut = await saying
        await agent.say_append(handle, "Stop.")
        result = await agent.say_close(handle)

    assert cut["reason"] == "preempted"
    assert cut["heard_text"] == "One two three four,"
    assert result == {
        "reason": "drained",
        "played_ms": 50,
        "interrupted": False,
        "heard_text": "Stop.",
    }
    assert fake.decode(kitchen.speaker.pcm()).text.endswith("Stop.")


async def test_once_a_streamed_say_is_cut_its_text_is_ignored_and_close_replies_at_once(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD, speech=SPEECH)
    device, speaker = await online(hub)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await command(device)  # the Status: agent
        handle = await agent.say_open(speaker)
        start = await command(device)
        device.send(Reply(start.id, {"format": start.args["format"]}))
        await agent.say_append(handle, LONG + " Nine")
        await asyncio.sleep(0.05)  # for its clauses to be fed
        stats = {"played_ms": 250, "lost": 0, "underruns": 0, "underrun_ms": 0}
        stopped = {"stream": start.args["stream"], "reason": "reflex"} | stats
        device.send(Event(0, "speaker", "stopped", 0, stopped))
        ended = await anext(n async for n in agent.notifications() if n.method == "say.ended")
        await agent.say_append(handle, " ten.")
        result = await agent.say_close(handle)

    assert result == {
        "reason": "reflex",
        "played_ms": 250,
        "interrupted": True,
        "heard_text": "One two three four,",
    }
    assert ended.params | {"t": 0} == {"address": speaker, "text": LONG + " Nine", **result, "t": 0}


@pytest.mark.parametrize(
    ("text", "params", "register"),
    [
        ("Привет, как твои дела? Hello.", {}, REGISTERS["masha"]),  # by the first clause
        ("Hello there, how are you?", {"lang": "ru"}, REGISTERS["masha"]),
        ("Hello there, how are you?", {"voice": "tenor"}, REGISTERS["tenor"]),
    ],
)
async def test_a_streamed_say_speaks_in_the_voice_its_params_or_its_first_clause_pick(
    hub: Hub,
    sim: Simulator,
    owner: OwnerClient,
    text: str,
    params: VoiceParams,
    register: int,
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.say_open("kitchen/speaker", **params)
        for word in text.split(" "):
            await agent.say_append(handle, word + " ")
        await agent.say_close(handle)

    assert fake.decode(kitchen.speaker.pcm()).registers == {register}


async def test_say_open_refuses_what_say_would_and_a_handle_that_isnt_open(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        codes: list[str] = []
        for refused in (
            agent.say_open("kitchen/speaker", voice="bass"),
            agent.say_open("kitchen/speaker", lang="de"),
            agent.say_open("kitchen/mic"),
            agent.say_append(7, "Hello."),
            agent.say_close(7),
        ):
            with pytest.raises(tinyaisense.ApiError) as error:
                await refused
            codes.append(error.value.code)
        handle = await agent.say_open("kitchen/speaker")
        await agent.say_close(handle)
        with pytest.raises(tinyaisense.ApiError) as closed:
            await agent.say_append(handle, "Hello.")

    assert codes == ["invalid_args", "unsupported", "unsupported", "disconnected", "disconnected"]
    assert closed.value.code == "disconnected"
    assert kitchen.speaker.captured == []


async def test_say_open_is_disconnected_when_the_device_is_offline(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await kitchen.close()
        async for notification in agent.notifications():
            if notification.method == "device.offline":
                break
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.say_open("kitchen/speaker")

    assert refused.value.code == "disconnected"


async def test_say_open_is_unsupported_with_no_tts(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    hub = await start_hub()
    async with OwnerClient(hub.owner_socket) as owner:
        await paired(simulator(hub.device_address), owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.say_open("kitchen/speaker")

    assert refused.value.code == "unsupported"


async def test_when_a_streamed_says_speaker_fails_to_start_append_and_close_say_how(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD, speech=SPEECH)
    device, speaker = await online(hub)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await command(device)  # the Status: agent
        handle = await agent.say_open(speaker)
        start = await command(device)
        device.send(FailedReply(start.id, "failed", "the amp is off"))
        await asyncio.sleep(0.05)  # for the failure to reach the say
        with pytest.raises(tinyaisense.ApiError) as appending:
            await agent.say_append(handle, "Hello.")
        with pytest.raises(tinyaisense.ApiError) as closing:
            await agent.say_close(handle)

    assert appending.value.code == closing.value.code == "failed"
