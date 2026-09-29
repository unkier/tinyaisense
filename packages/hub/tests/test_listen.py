"""`listen` and Utterances through the speech layer, with the fake speech plugin:
docs/protocol/agent.md#utterances.

The Simulator's mic hears text spoken as the fake TTS's tones, which the fake STT decodes
back. The fake end-of-turn model hears a turn end at a full stop or a question mark.
"""

import asyncio
import contextlib
from dataclasses import replace

import pytest
import tinyaisense
from tinyaisense_hub import EngineConfig, Hub, OwnerClient, SpeechConfig, Timing
from tinyaisense_hub.speech import fake
from tinyaisense_sim import SimDevice, Simulator, sources

from .conftest import FAST_TIMING, StartHub
from .test_say import SPEECH as SAY_SPEECH

ECHO = "m5stack-atom-echo"
TOKEN = "agent-token"

SPEECH = replace(
    SAY_SPEECH,
    engines=SAY_SPEECH.engines
    | {
        "vad": EngineConfig("tinyaisense_hub.speech.fake:FakeVad"),
        "turn": EngineConfig("tinyaisense_hub.speech.fake:FakeTurn"),
        "stt": EngineConfig("tinyaisense_hub.speech.fake:FakeStt"),
    },
    vad="vad",
    turn="turn",
    stt={"en": "stt", "ru": "stt"},
)
TIMING = FAST_TIMING


@pytest.fixture
async def hub(start_hub: StartHub, request: pytest.FixtureRequest) -> Hub:
    """With `SPEECH` and `TIMING`; parametrize it indirectly with a `Timing` or a
    `SpeechConfig` for others."""
    param: object = getattr(request, "param", None)
    return await start_hub(
        speech=param if isinstance(param, SpeechConfig) else SPEECH,
        timing=param if isinstance(param, Timing) else TIMING,
    )


async def paired(sim: Simulator, owner: OwnerClient, name: str = "kitchen") -> SimDevice:
    device = await sim.add(ECHO, name)
    await owner.approve(device.device_id, name)
    await device.wait_for(lambda: device.state == "online")
    return device


def says(text: str, silence_ms: int = 300) -> bytes:
    """What the human says, after some silence: `text` as the fake TTS speaks it."""
    return sources.silence(silence_ms) + fake.encode(text)


async def test_listen_replies_with_what_the_human_said_once_their_turn_is_over(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("Hello there, how are you?"))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        result = await agent.listen("kitchen/mic")

    assert result | {"utterance_id": 0, "stats": {}} == {
        "utterance_id": 0,
        "reason": "end_of_turn",
        "transcript": "Hello there, how are you?",
        "lang": "en",
        "stats": {},
    }
    assert result["stats"]["lost"] == 0


async def utterance(agent: tinyaisense.Session) -> list[tinyaisense.Notification]:
    """The Utterance's notifications, up to its `utterance.end`."""
    heard: list[tinyaisense.Notification] = []
    async for notification in agent.notifications():
        if notification.method.startswith(("utterance.", "transcript.")):
            heard.append(notification)
        if notification.method == "utterance.end":
            break
    return heard


async def shows(device: SimDevice, status: str) -> None:
    """Wait until `device` shows `status`."""
    async with asyncio.timeout(5):
        await device.wait_for(lambda: device.status == status)


def summary(heard: list[tinyaisense.Notification]) -> list[tuple[str, object]]:
    return [
        (n.method, n.params.get("text", n.params.get("reason", n.params.get("trigger"))))
        for n in heard
    ]


async def test_each_segment_comes_as_a_partial_and_the_lang_is_the_longest_segments(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(
        says("Да, конечно, всё хорошо, спасибо,")
        + says("I am fine, thank you, and how are you doing?", silence_ms=500)
    )
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        result = await agent.listen("kitchen/mic")
        heard = await utterance(agent)

    assert summary(heard) == [
        ("utterance.start", "listen"),
        ("transcript.partial", "Да, конечно, всё хорошо, спасибо,"),
        ("transcript.partial", "I am fine, thank you, and how are you doing?"),
        ("utterance.end", "end_of_turn"),
    ]
    start, first, second, end = (n.params for n in heard)
    assert start["address"] == "kitchen/mic"
    assert "handle" not in start
    assert (first["lang"], second["lang"]) == ("ru", "en")
    assert {first["utterance_id"], second["utterance_id"]} == {start["utterance_id"]}
    assert end | {"t": 0} == result | {"t": 0}
    assert (
        result["transcript"]
        == "Да, конечно, всё хорошо, спасибо, I am fine, thank you, and how are you doing?"
    )
    assert result["lang"] == "en"


async def test_an_unfinished_turn_ends_after_a_longer_silence(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("So, well, I was thinking,"))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        result = await agent.listen("kitchen/mic", lang="en")

    assert (result["reason"], result["transcript"], result["lang"]) == (
        "end_of_turn",
        "So, well, I was thinking,",
        "en",
    )
    # 300 ms of silence, 250 ms of speech, then the turn's silence.
    assert result["stats"]["duration_ms"] >= 300 + 250 + 1000


@pytest.mark.parametrize("hub", [replace(TIMING, no_speech_ms=300)], indirect=True)
async def test_a_listen_nobody_speaks_to_ends_no_speech(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        result = await agent.listen("kitchen/mic")

    assert (result["reason"], result["transcript"]) == ("no_speech", "")
    assert "lang" not in result


@pytest.mark.parametrize("hub", [replace(TIMING, listen_max_ms=1000)], indirect=True)
async def test_a_listen_ends_at_its_max_duration_with_what_was_said(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("Well, I was going to say,"))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        result = await agent.listen("kitchen/mic")

    assert (result["reason"], result["transcript"]) == ("max_duration", "Well, I was going to say,")


async def test_cancelling_a_listen_ends_it_stopped_with_what_was_heard(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("Wait, let me think about it,"))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        listening = asyncio.create_task(agent.listen("kitchen/mic"))
        notifications = agent.notifications()
        assert (await anext(notifications)).method == "utterance.start"
        assert (await anext(notifications)).params["text"] == "Wait, let me think about it,"

        listening.cancel()
        end = await anext(notifications)

    assert end.method == "utterance.end"
    assert (end.params["reason"], end.params["transcript"]) == (
        "stopped",
        "Wait, let me think about it,",
    )
    assert kitchen.mic.stream is None


async def test_a_device_that_drops_mid_listen_delivers_what_was_heard_disconnected(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("Hang on, I need a moment,"))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        listening = asyncio.create_task(agent.listen("kitchen/mic"))
        notifications = agent.notifications()
        assert (await anext(notifications)).method == "utterance.start"
        assert (await anext(notifications)).params["text"] == "Hang on, I need a moment,"

        kitchen.faults.rst()
        result = await listening

    assert (result["reason"], result["transcript"]) == ("disconnected", "Hang on, I need a moment,")


async def test_a_session_that_ends_delivers_its_listens_disconnected_first(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("Hold on, one second please,"))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as old:
        listening = asyncio.create_task(old.listen("kitchen/mic"))
        notifications = old.notifications()
        assert (await anext(notifications)).method == "utterance.start"
        assert (await anext(notifications)).params["text"] == "Hold on, one second please,"

        async with tinyaisense.connect(hub.agent_url, TOKEN, takeover=True):
            assert kitchen.mic.stream is None
            rest = [n async for n in notifications]
        with contextlib.suppress(tinyaisense.SessionEnded):
            await listening  # its reply may make it before `session.ended`, or not

    assert summary(rest) == [("utterance.end", "disconnected"), ("session.ended", "taken_over")]
    assert rest[0].params["transcript"] == "Hold on, one second please,"


async def test_a_listen_can_ask_for_the_audio_at_its_own_rate_and_no_transcript(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("Hi there, nice to meet you."))
    opt_in = {"audio": True, "rate": 24000}
    async with tinyaisense.connect(hub.agent_url, TOKEN, utterances=opt_in) as agent:
        result = await agent.listen("kitchen/mic", utterances={"transcript": False})
        heard = await utterance(agent)
        handle = heard[0].params["handle"]
        frames = [frame async for frame in agent.audio(handle)]

    assert summary(heard) == [("utterance.start", "listen"), ("utterance.end", "end_of_turn")]
    assert set(result) == {"utterance_id", "reason", "stats", "t"} - {"t"}
    assert [frame.index for frame in frames] == list(range(result["stats"]["duration_ms"] // 10))
    assert {len(frame.pcm) for frame in frames} == {240 * 2}


@pytest.mark.parametrize("hub", [SAY_SPEECH], indirect=True)
async def test_listen_is_unsupported_without_an_stt_engine(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.listen("kitchen/mic")

    assert refused.value.code == "unsupported"


async def test_listen_is_unsupported_on_a_speaker(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.listen("kitchen/speaker")

    assert refused.value.code == "unsupported"


async def test_then_listen_listens_once_the_say_has_played(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("Yes, I would love some tea."))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        result = await agent.say("kitchen/speaker", "Would you like tea?", then_listen=True)
        heard = await utterance(agent)

    assert result["reason"] == "drained"
    assert summary(heard) == [
        ("utterance.start", "then_listen"),
        ("transcript.partial", "Yes, I would love some tea."),
        ("utterance.end", "end_of_turn"),
    ]
    assert {n.params["utterance_id"] for n in heard} == {result["utterance_id"]}
    assert heard[0].params["address"] == "kitchen/mic"


async def test_then_listen_on_a_streamed_say_listens_once_it_has_played(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("Tomorrow, around noon."))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.say_open("kitchen/speaker", then_listen={"lang": "en"})
        await agent.say_append(handle, "When should we meet?")
        result = await agent.say_close(handle)
        heard = await utterance(agent)

    assert summary(heard)[-1] == ("utterance.end", "end_of_turn")
    assert heard[-1].params["utterance_id"] == result["utterance_id"]
    assert heard[-1].params["transcript"] == "Tomorrow, around noon."


async def test_then_listen_raw_opens_a_raw_mic_stream_once_the_say_has_played(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        result = await agent.say("kitchen/speaker", "Go ahead.", then_listen={"raw": True})
        notifications = agent.notifications()
        started = await anext(n async for n in notifications if n.method == "stream.started")
        frames = agent.audio(started.params["handle"])
        await anext(frames)
        stopped = await agent.stream_stop(started.params["handle"])

    assert "utterance_id" not in result
    assert started.params["address"] == "kitchen/mic"
    assert stopped["reason"] == "stopped"
    assert kitchen.mic.stream is None


async def test_a_say_cut_before_it_played_doesnt_listen(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        first = asyncio.create_task(
            agent.say("kitchen/speaker", "A long question " * 10, then_listen=True)
        )
        notifications = agent.notifications()
        await asyncio.sleep(0.2)
        await agent.say("kitchen/speaker", "Never mind.", replace=True)
        cut = await first
        methods: list[str] = []
        async for notification in notifications:
            methods.append(notification.method)
            if notification.method == "say.ended" and notification.params["text"] == "Never mind.":
                break

    assert cut["reason"] == "preempted"
    assert "utterance_id" not in cut
    assert methods == ["say.ended", "say.ended"]


@pytest.mark.parametrize("hub", [SAY_SPEECH], indirect=True)
async def test_then_listen_is_unsupported_without_an_stt_engine(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.say("kitchen/speaker", "Hello.", then_listen=True)

    assert refused.value.code == "unsupported"
    assert kitchen.speaker.stream is None


async def test_a_delivered_utterance_shows_thinking_until_the_agent_speaks(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("What time is it right now?"))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await agent.listen("kitchen/mic")
        await shows(kitchen, "thinking")

        await agent.say("kitchen/speaker", "Noon.")
        await shows(kitchen, "agent")


@pytest.mark.parametrize("hub", [replace(TIMING, thinking_ms=300)], indirect=True)
async def test_thinking_ends_by_itself_if_the_agent_does_nothing(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("What time is it right now?"))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await agent.listen("kitchen/mic")
        await shows(kitchen, "thinking")

        await shows(kitchen, "agent")


@pytest.mark.parametrize("hub", [replace(TIMING, no_speech_ms=300)], indirect=True)
async def test_listening_again_ends_thinking_and_hearing_nothing_doesnt_start_it(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("What time is it right now?", silence_ms=50))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await agent.listen("kitchen/mic")
        await shows(kitchen, "thinking")

        heard = await agent.listen("kitchen/mic")
        await asyncio.sleep(0.1)

        assert heard["reason"] == "no_speech"
        assert kitchen.status == "agent"


async def test_a_raw_mic_stream_never_shows_thinking(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("What time is it right now?"))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/mic")
        await asyncio.sleep(0.6)
        await agent.stream_stop(handle)
        await asyncio.sleep(0.1)

        assert kitchen.status == "agent"
