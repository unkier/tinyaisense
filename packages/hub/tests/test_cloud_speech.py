"""A cloud speech engine, with a local one standing in when it fails: the fake polza.ai, and
the fake speech plugin as the stand-in. docs/protocol/agent.md#speech.

The fake polza.ai speaks in its own register, so the device's audio tells which engine spoke.
"""

import asyncio
from dataclasses import replace

import pytest
import tinyaisense
from tinyaisense_hub import (
    ConfigError,
    EngineConfig,
    Hub,
    OwnerClient,
    SpeechConfig,
    VoiceConfig,
)
from tinyaisense_hub.speech import fake
from tinyaisense_sim import SimDevice, Simulator, sources
from tinyaisense_sim.pytest_plugin import SimulatorFactory

from .conftest import FAST_TIMING, StartHub
from .fake_polza import REGISTER, FakePolza

ECHO = "m5stack-atom-echo"
TOKEN = "agent-token"
LOCAL_REGISTER = 1
"""The stand-in's voice speaks in this register."""
TIMING = replace(FAST_TIMING, cloud_first_byte_ms=300, cloud_skip_ms=1000)


def speech(polza: FakePolza) -> SpeechConfig:
    return SpeechConfig(
        engines={
            "polza_tts": EngineConfig("tinyaisense_hub.speech.polza:PolzaTts", {"url": polza.url}),
            "local_tts": EngineConfig(
                "tinyaisense_hub.speech.fake:FakeTts",
                {"rate": 24000, "voices": {"tenor": LOCAL_REGISTER}},
            ),
            "polza_stt": EngineConfig("tinyaisense_hub.speech.polza:PolzaStt", {"url": polza.url}),
            "local_stt": EngineConfig("tinyaisense_hub.speech.fake:FakeStt"),
            "vad": EngineConfig("tinyaisense_hub.speech.fake:FakeVad"),
            "turn": EngineConfig("tinyaisense_hub.speech.fake:FakeTurn"),
        },
        tts={"en": ("polza_tts", "local_tts")},
        stt={"en": ("polza_stt", "local_stt")},
        vad="vad",
        turn="turn",
        voices=(VoiceConfig("nova", "en", "Nova", fallback_voice="tenor"),),
    )


@pytest.fixture
async def hub(start_hub: StartHub, polza: FakePolza) -> Hub:
    return await start_hub(speech=speech(polza), timing=TIMING)


@pytest.fixture
def sim(hub: Hub, simulator: SimulatorFactory) -> Simulator:
    return simulator(hub.device_address)


async def paired(sim: Simulator, owner: OwnerClient, name: str = "kitchen") -> SimDevice:
    device = await sim.add(ECHO, name)
    await owner.approve(device.device_id, name)
    await device.wait_for(lambda: device.state == "online")
    return device


def heard(device: SimDevice) -> fake.Decoded:
    """What the device played, less its silences."""
    decoded = fake.decode(device.speaker.pcm())
    return replace(decoded, text=decoded.text.replace(fake.SILENCE, ""))


async def test_say_speaks_with_the_cloud_engine_while_it_answers(
    hub: Hub, sim: Simulator, owner: OwnerClient, polza: FakePolza
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        result = await agent.say("kitchen/speaker", "Hello there.")

    assert result == {
        "reason": "drained",
        "played_ms": 121,  # resampled from 24 kHz, which flushes 1 ms more
        "interrupted": False,
        "heard_text": "Hello there.",
    }
    assert heard(kitchen) == fake.Decoded("Hello there.", frozenset({REGISTER}))
    assert [(r.path, r.body["voice"]) for r in polza.requests] == [("/api/v1/audio/speech", "nova")]


async def test_when_the_cloud_engine_fails_the_stand_in_speaks_and_say_is_degraded(
    hub: Hub, sim: Simulator, owner: OwnerClient, polza: FakePolza
) -> None:
    kitchen = await paired(sim, owner)
    polza.status = 503
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        result = await agent.say("kitchen/speaker", "Hello there.")

    assert result == {
        "reason": "drained",
        "played_ms": 121,
        "interrupted": False,
        "heard_text": "Hello there.",
        "degraded": True,
    }
    assert heard(kitchen) == fake.Decoded("Hello there.", frozenset({LOCAL_REGISTER}))
    assert len(polza.requests) == 1


async def test_a_cloud_engine_with_no_first_byte_in_time_is_stood_in_for_at_that_time(
    hub: Hub, sim: Simulator, owner: OwnerClient, polza: FakePolza
) -> None:
    kitchen = await paired(sim, owner)
    polza.delay = 1.0
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        started = asyncio.get_running_loop().time()
        result = await agent.say("kitchen/speaker", "Hi.")
        took = asyncio.get_running_loop().time() - started

    assert result["degraded"] is True
    assert heard(kitchen) == fake.Decoded("Hi.", frozenset({LOCAL_REGISTER}))
    # 300 ms of waiting for the cloud, then 31 ms of speech: long before the cloud answers.
    assert 0.3 < took < 0.8


async def test_a_reply_the_cloud_engine_gave_up_waiting_for_holds_up_no_later_call(
    hub: Hub, sim: Simulator, owner: OwnerClient, polza: FakePolza
) -> None:
    await paired(sim, owner)
    polza.delay = 1.0
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        slow = await agent.say("kitchen/speaker", "Hi.")
        polza.delay = 0.0
        # The cloud is still on the slow reply: it answers the next call at once all the same.
        prompt = await agent.say("kitchen/speaker", "Hi.")

    assert slow["degraded"] is True
    assert "degraded" not in prompt
    assert len(polza.requests) == 2


async def test_after_3_failures_in_a_row_the_cloud_engine_is_skipped_for_cloud_skip_ms(
    hub: Hub, sim: Simulator, owner: OwnerClient, polza: FakePolza
) -> None:
    await paired(sim, owner)
    polza.status = 503
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        failed = [await agent.say("kitchen/speaker", "Hi.") for _ in range(3)]
        polza.status = 200
        skipped = await agent.say("kitchen/speaker", "Hi.")
        asked = len(polza.requests)
        await asyncio.sleep(TIMING.cloud_skip_ms / 1000)
        retried = await agent.say("kitchen/speaker", "Hi.")

    assert [result["degraded"] for result in [*failed, skipped]] == [True] * 4
    assert asked == 3
    assert "degraded" not in retried
    assert len(polza.requests) == 4


ASKED = "Hello there, how are you?"
"""250 ms of speech: a segment, which takes at least 200 ms."""


def says(text: str) -> bytes:
    """What the human says, after some silence: `text` as the fake TTS speaks it."""
    return sources.silence(300) + fake.encode(text)


async def test_listen_hears_with_the_cloud_engine_while_it_answers(
    hub: Hub, sim: Simulator, owner: OwnerClient, polza: FakePolza
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says(ASKED))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        result = await agent.listen("kitchen/mic")

    assert (result["reason"], result["transcript"], result["lang"]) == ("end_of_turn", ASKED, "en")
    assert "degraded" not in result
    assert [r.path for r in polza.requests] == ["/api/v1/audio/transcriptions"]


async def test_when_the_cloud_engine_fails_the_stand_in_hears_and_the_utterance_is_degraded(
    hub: Hub, sim: Simulator, owner: OwnerClient, polza: FakePolza
) -> None:
    kitchen = await paired(sim, owner)
    polza.status = 503
    kitchen.mic.feed(says(ASKED))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        result = await agent.listen("kitchen/mic")
        async for notification in notifications:
            if notification.method == "utterance.end":
                ended = notification.params
                break
        else:
            raise AssertionError("no utterance.end")

    assert (result["reason"], result["transcript"]) == ("end_of_turn", ASKED)
    assert result["degraded"] is True
    assert ended["degraded"] is True
    assert len(polza.requests) == 1


async def test_a_segment_the_cloud_engine_is_slow_on_is_heard_by_the_stand_in_at_that_time(
    hub: Hub, sim: Simulator, owner: OwnerClient, polza: FakePolza
) -> None:
    kitchen = await paired(sim, owner)
    polza.delay = 1.0
    kitchen.mic.feed(says(ASKED))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        started = asyncio.get_running_loop().time()
        result = await agent.listen("kitchen/mic")
        took = asyncio.get_running_loop().time() - started

    assert (result["transcript"], result["degraded"]) == (ASKED, True)
    # 300 ms of silence, 250 ms of speech, 200 ms of silence to end it, then 300 ms of
    # waiting for the cloud: long before the cloud answers.
    assert 1.05 < took < 1.5


async def test_the_hub_refuses_to_start_with_a_voice_the_stand_in_hasnt(
    start_hub: StartHub, polza: FakePolza
) -> None:
    no_fallback_voice = replace(speech(polza), voices=(VoiceConfig("nova", "en", "Nova"),))

    with pytest.raises(ConfigError) as refused:
        await start_hub(speech=no_fallback_voice, timing=TIMING)

    assert str(refused.value) == (
        "Voice 'nova': speech engine 'local_tts' has no voice 'nova': set its fallback_voice"
    )


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (
            {"tts": {"en": "polza_tts"}},
            "speech engine 'polza_tts' is a cloud engine: a local one must stand in for it",
        ),
        (
            {"stt": {"en": ("local_stt", "polza_stt")}},
            "speech engine 'local_stt' isn't a cloud engine: it needs no stand-in",
        ),
        (
            {"tts": {"en": ("polza_tts", "polza_tts_too")}},
            "speech engine 'polza_tts_too' is a cloud engine: it can't stand in",
        ),
    ],
)
async def test_the_hub_refuses_to_start_with_a_cloud_engine_without_a_local_stand_in(
    start_hub: StartHub, polza: FakePolza, change: dict[str, object], error: str
) -> None:
    config = speech(polza)
    engines = config.engines | {"polza_tts_too": config.engines["polza_tts"]}

    with pytest.raises(ConfigError) as refused:
        await start_hub(speech=replace(config, engines=engines, **change), timing=TIMING)

    assert str(refused.value) == error


@pytest.mark.parametrize(
    ("name", "params", "error"),
    [
        ("local_tts", {"languages": ["ru"]}, "speech engine 'local_tts' doesn't speak en, only ru"),
        (
            "local_stt",
            {"rate": 24000},
            "speech engine 'local_stt' hears at 24000 Hz: the hub listens at 16000 Hz only",
        ),
        (
            "polza_stt",
            {"key_env": "NO_SUCH_KEY"},
            "speech engine 'polza_stt': polza.ai needs its API key in env NO_SUCH_KEY",
        ),
    ],
)
async def test_the_hub_refuses_to_start_with_a_cloud_engine_or_stand_in_that_cant_serve(
    start_hub: StartHub, polza: FakePolza, name: str, params: dict[str, object], error: str
) -> None:
    config = speech(polza)
    engine = config.engines[name]
    engines = config.engines | {name: replace(engine, params=engine.params | params)}

    with pytest.raises(ConfigError) as refused:
        await start_hub(speech=replace(config, engines=engines), timing=TIMING)

    assert str(refused.value) == error
