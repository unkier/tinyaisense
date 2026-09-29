"""`say` through the speech layer, with the fake speech plugin: docs/protocol/agent.md#say.

The fake TTS speaks each character as a 10 ms tone, so the audio the Simulator heard decodes
back into the text that was spoken.
"""

import asyncio
import itertools
import json
from dataclasses import replace
from typing import TypedDict

import pytest
import tinyaisense
import websockets
from tinyaisense_hub import (
    ConfigError,
    EngineConfig,
    Hub,
    OwnerClient,
    SpeechConfig,
    VoiceConfig,
)
from tinyaisense_hub.speech import fake
from tinyaisense_sim import SimDevice, Simulator
from tinyaisense_sim.pytest_plugin import SimulatorFactory
from tinyaisense_wire import Event, Reply

from .conftest import StartHub
from .test_speaker_streams import NEVER_DEAD, command, online

ECHO = "m5stack-atom-echo"
TOKEN = "agent-token"

LONG = "One two three four, five six seven eight."
"""Its first clause ends at 190 ms, its second at 400 ms."""
REGISTERS = {"alto": 0, "tenor": 1, "masha": 2}
"""Each fake voice's register: what tells them apart in the audio."""
SPEECH = SpeechConfig(
    engines={
        "fake": EngineConfig("tinyaisense_hub.speech.fake:FakeTts", {"voices": REGISTERS}),
    },
    tts={"en": "fake", "ru": "fake"},
    voices=(
        VoiceConfig("alto", "en", "Alto"),
        VoiceConfig("tenor", "en", "Tenor"),
        VoiceConfig("masha", "ru", "Маша"),
    ),
)


class VoiceParams(TypedDict, total=False):
    voice: str
    lang: str


@pytest.fixture
async def hub(start_hub: StartHub) -> Hub:
    return await start_hub(speech=SPEECH)


def gaps(device: SimDevice) -> list[float]:
    """Where the device's speaker went quiet between frames, in ms: an underrun skips a
    10 ms slot. A late wakeup of its playout clock delays a frame by a ms or two only."""
    at = [frame.at for frame in device.speaker.captured]
    return [round((b - a) * 1000) for a, b in itertools.pairwise(at) if b - a > 0.015]


async def paired(sim: Simulator, owner: OwnerClient, name: str = "kitchen") -> SimDevice:
    device = await sim.add(ECHO, name)
    await owner.approve(device.device_id, name)
    await device.wait_for(lambda: device.state == "online")
    return device


async def test_the_device_speaks_the_text_and_say_replies_once_it_has_played(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        result = await agent.say("kitchen/speaker", "Hello there.")

    assert result == {
        "reason": "drained",
        "played_ms": 120,
        "interrupted": False,
        "heard_text": "Hello there.",
    }
    assert fake.decode(kitchen.speaker.pcm()).text == "Hello there."


async def test_say_ended_goes_out_just_before_the_reply_and_the_says_stream_is_unseen(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    await paired(sim, owner)
    hello = {"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {"token": TOKEN}}
    say = {"address": "speaker", "text": "Hi there."}
    async with websockets.connect(hub.agent_url) as ws:
        await ws.send(json.dumps(hello))
        await ws.recv()
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "say", "params": say}))

        async with asyncio.timeout(5):
            ended, reply = [json.loads(await ws.recv()) for _ in range(2)]

    assert ended["method"] == "say.ended"
    assert ended["params"] | {"t": 0} == {
        "address": "kitchen/speaker",
        "text": "Hi there.",
        **reply["result"],
        "t": 0,
    }
    assert reply["id"] == 2


async def test_voices_lists_the_voices_of_the_config_by_id_and_never_by_engine(hub: Hub) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        voices = await agent.voices()

    assert voices == [
        {"id": "alto", "lang": "en", "name": "Alto"},
        {"id": "tenor", "lang": "en", "name": "Tenor"},
        {"id": "masha", "lang": "ru", "name": "Маша"},
    ]


async def test_a_hub_with_no_tts_has_no_voices_and_say_is_unsupported(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    hub = await start_hub()
    sim = simulator(hub.device_address)
    async with OwnerClient(hub.owner_socket) as owner:
        await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        assert await agent.voices() == []
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.say("kitchen/speaker", "Hello there.")

    assert refused.value.code == "unsupported"


async def test_the_tts_hears_no_markdown_emoji_or_urls_but_heard_text_is_the_original(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    text = "**Hello** there 🎉, see https://x.io"
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        result = await agent.say("kitchen/speaker", text)

    assert fake.decode(kitchen.speaker.pcm()).text == "Hello there, see"
    assert result["heard_text"] == text
    assert result["played_ms"] == 160


@pytest.mark.parametrize(
    ("text", "params", "register"),
    [
        ("Hello there.", {}, REGISTERS["alto"]),  # the default Voice of the text's language
        ("Привет, как дела?", {}, REGISTERS["masha"]),
        ("Hello there.", {"lang": "ru"}, REGISTERS["masha"]),
        ("Hello there.", {"voice": "tenor"}, REGISTERS["tenor"]),
        ("Hello there.", {"voice": "masha", "lang": "ru"}, REGISTERS["masha"]),
        ("42!", {}, REGISTERS["alto"]),  # no letters: the hub's default language
    ],
)
async def test_say_speaks_in_the_voice_its_params_or_its_text_pick(
    hub: Hub,
    sim: Simulator,
    owner: OwnerClient,
    text: str,
    params: VoiceParams,
    register: int,
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await agent.say("kitchen/speaker", text, **params)

    assert fake.decode(kitchen.speaker.pcm()) == fake.Decoded(text, frozenset({register}))


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"voice": "bass"}, "invalid_args"),
        ({"voice": "masha", "lang": "en"}, "invalid_args"),
        ({"lang": "de"}, "unsupported"),
    ],
)
async def test_say_refuses_a_voice_or_language_the_hub_doesnt_have(
    hub: Hub, sim: Simulator, owner: OwnerClient, params: VoiceParams, code: str
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.say("kitchen/speaker", "Hello there.", **params)

    assert refused.value.code == code
    assert kitchen.speaker.captured == []


async def test_queued_says_play_back_to_back_on_one_stream_each_replying_as_it_ends(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        results = await asyncio.gather(
            agent.say("kitchen/speaker", "One two."),
            agent.say("kitchen/speaker", "Three four five."),
            agent.say("kitchen/speaker", "Six."),
        )

    assert fake.decode(kitchen.speaker.pcm()).text == "One two.Three four five.Six."
    assert len({frame.stream for frame in kitchen.speaker.captured}) == 1
    assert gaps(kitchen) == []
    assert results == [
        {"reason": "drained", "played_ms": ms, "interrupted": False, "heard_text": text}
        for ms, text in [(80, "One two."), (160, "Three four five."), (40, "Six.")]
    ]


async def test_a_replacing_say_cuts_in_and_the_says_it_cut_heard_only_what_played(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    first = "One two three four, five six seven eight nine ten eleven twelve. Thirteen."
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        cut = asyncio.ensure_future(agent.say("kitchen/speaker", first))
        queued = asyncio.ensure_future(agent.say("kitchen/speaker", "Never heard."))
        # Past the first clause, 190 ms, well into the second, which ends at 630 ms. The
        # stop takes back what's still in the device's DMA, 30 ms.
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 35)
        replacing = await agent.say("kitchen/speaker", "Stop.", replace=True)
        cut_result, queued_result = await cut, await queued

    assert cut_result | {"played_ms": 0} == {
        "reason": "preempted",
        "played_ms": 0,
        "interrupted": True,
        "heard_text": "One two three four,",
    }
    assert 190 < cut_result["played_ms"] < 630
    assert queued_result == {
        "reason": "preempted",
        "played_ms": 0,
        "interrupted": True,
        "heard_text": "",
    }
    assert replacing == {
        "reason": "drained",
        "played_ms": 50,
        "interrupted": False,
        "heard_text": "Stop.",
    }
    heard = fake.decode(kitchen.speaker.pcm()).text
    assert heard.startswith("One two three four,five")
    assert heard.endswith("Stop.")
    assert "Never" not in heard


async def test_a_raw_stream_on_the_speaker_preempts_a_say(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        saying = asyncio.ensure_future(agent.say("kitchen/speaker", LONG))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 30)
        handle = await agent.stream_start("kitchen/speaker")
        result = await saying
        await agent.stream_stop(handle)

    assert result | {"played_ms": 0} == {
        "reason": "preempted",
        "played_ms": 0,
        "interrupted": True,
        "heard_text": "One two three four,",
    }
    assert 190 < result["played_ms"] < 400


async def test_a_say_preempts_a_raw_stream_on_the_speaker(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/speaker")
        result = await agent.say("kitchen/speaker", "Hi.")
        methods: dict[str, tinyaisense.Notification] = {}
        async for notification in agent.notifications():
            methods[notification.method] = notification
            if notification.method == "say.ended":
                break

    assert methods["stream.ended"].params["handle"] == handle
    assert methods["stream.ended"].params["reason"] == "preempted"
    assert result["reason"] == "drained"


async def test_a_say_ends_disconnected_when_its_device_goes_offline(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        saying = asyncio.ensure_future(agent.say("kitchen/speaker", LONG))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 5)
        await kitchen.close()
        result = await saying

    assert result["reason"] == "disconnected"
    assert result["interrupted"]
    assert result["played_ms"] < 400


async def test_a_takeover_cuts_the_old_sessions_say_and_the_new_one_speaks_at_once(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    old = await tinyaisense.connect(hub.agent_url, TOKEN)
    notifications = old.notifications()
    saying = asyncio.ensure_future(old.say("kitchen/speaker", LONG))
    await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 5)
    async with tinyaisense.connect(hub.agent_url, TOKEN, takeover=True) as new:
        result = await new.say("kitchen/speaker", "Hi.")

    ended = [n async for n in notifications]
    await asyncio.gather(saying, return_exceptions=True)  # its reply may beat the link's end
    await old.close()
    assert [(n.method, n.params.get("reason")) for n in ended] == [
        ("say.ended", "disconnected"),
        ("session.ended", "taken_over"),
    ]
    assert result == {
        "reason": "drained",
        "played_ms": 30,
        "interrupted": False,
        "heard_text": "Hi.",
    }
    assert fake.decode(kitchen.speaker.pcm()).text.endswith("Hi.")


async def test_a_reflex_cut_ends_the_say_playing_and_the_ones_queued_reflex(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD, speech=SPEECH)
    device, speaker = await online(hub)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await command(device)  # the Status: agent
        await command(device)  # arming the Reflex
        cut = asyncio.ensure_future(agent.say(speaker, LONG))
        queued = asyncio.ensure_future(agent.say(speaker, "Never heard."))
        start = await command(device)
        device.send(Reply(start.id, {"format": start.args["format"]}))
        await asyncio.sleep(0.05)  # for the stream to start, and the second say to join
        stats = {"played_ms": 250, "lost": 0, "underruns": 0, "underrun_ms": 0}
        stopped = {"stream": start.args["stream"], "reason": "reflex"} | stats
        device.send(Event(0, "speaker", "stopped", 0, stopped))
        results = [await cut, await queued]

    assert results == [
        {
            "reason": "reflex",
            "played_ms": 250,
            "interrupted": True,
            "heard_text": "One two three four,",
        },
        {"reason": "reflex", "played_ms": 0, "interrupted": True, "heard_text": ""},
    ]


async def test_a_tts_at_24_khz_is_resampled_to_the_devices_16_khz(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    fake24 = EngineConfig("tinyaisense_hub.speech.fake:FakeTts", {"rate": 24000})
    hub = await start_hub(speech=replace(SPEECH, engines={"fake": fake24}))
    sim = simulator(hub.device_address)
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        result = await agent.say("kitchen/speaker", "Hello there.")

    assert fake.decode(kitchen.speaker.pcm()).text == "Hello there."
    assert result["played_ms"] == 120


async def test_a_tts_at_a_rate_the_hub_cant_resample_fails_the_hub_start(
    start_hub: StartHub,
) -> None:
    odd = EngineConfig("tinyaisense_hub.speech.fake:FakeTts", {"rate": 22050})

    with pytest.raises(ConfigError, match="22050"):
        await start_hub(speech=replace(SPEECH, engines={"fake": odd}))


async def test_a_say_is_synthesized_3_s_ahead_of_playback_and_no_further_once_cut(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    journal = EngineConfig(
        "tinyaisense_hub.speech.fake:FakeTts", {"voices": REGISTERS, "journal": "ahead"}
    )
    hub = await start_hub(speech=replace(SPEECH, engines={"fake": journal}))
    sim = simulator(hub.device_address)
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(sim, owner)
    sentences = [f"Sentence number {n} is {'a' * 78}." for n in range(10)]  # 1 s each
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        cut = asyncio.ensure_future(agent.say("kitchen/speaker", " ".join(sentences)))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 30)
        await agent.say("kitchen/speaker", "Stop.", replace=True)
        await cut

    # Up to 3 s unsent: the fourth sentence goes past it, and the cut came before 1 s more
    # had played.
    assert fake.JOURNALS["ahead"] == [*sentences[:4], "Stop."]


async def test_a_say_that_comes_as_the_speaker_drains_plays_right_after_on_a_new_stream(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        first = asyncio.ensure_future(agent.say("kitchen/speaker", "Hi."))
        # 30 ms go out in the first burst, so the stream drains as soon as it starts.
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 1)
        second = await agent.say("kitchen/speaker", "Bye.")

    assert (await first)["reason"] == "drained"
    assert second == {
        "reason": "drained",
        "played_ms": 40,
        "interrupted": False,
        "heard_text": "Bye.",
    }
    assert fake.decode(kitchen.speaker.pcm()).text == "Hi.Bye."
    assert len({frame.stream for frame in kitchen.speaker.captured}) == 2


async def test_a_replacing_say_cuts_a_say_waiting_for_the_speaker_to_drain(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        first = asyncio.ensure_future(agent.say("kitchen/speaker", "Hi."))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 1)
        waiting, replacing = await asyncio.gather(  # sent in this order
            agent.say("kitchen/speaker", "Never heard."),
            agent.say("kitchen/speaker", "Bye.", replace=True),
        )

    assert (await first)["reason"] in ("drained", "preempted")  # it may have drained by then
    assert waiting == {
        "reason": "preempted",
        "played_ms": 0,
        "interrupted": True,
        "heard_text": "",
    }
    assert replacing["reason"] == "drained"
    assert "Never" not in fake.decode(kitchen.speaker.pcm()).text


async def test_a_say_queued_while_another_plays_follows_it_with_no_gap(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        first = asyncio.ensure_future(agent.say("kitchen/speaker", LONG))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 10)
        second = await agent.say("kitchen/speaker", "Bye.")

    assert (await first)["reason"] == "drained"
    assert second["reason"] == "drained"
    assert fake.decode(kitchen.speaker.pcm()).text == "One two three four,five six seven eight.Bye."
    assert len({frame.stream for frame in kitchen.speaker.captured}) == 1
    assert gaps(kitchen) == []


async def test_a_say_with_nothing_to_speak_ends_at_once_but_still_cuts_in_with_replace(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        nothing = await agent.say("kitchen/speaker", "🎉")
        cut = asyncio.ensure_future(agent.say("kitchen/speaker", LONG))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 5)
        replacing = await agent.say("kitchen/speaker", "https://x.io", replace=True)

    silent = {"reason": "drained", "played_ms": 0, "interrupted": False, "heard_text": ""}
    assert nothing == silent
    assert replacing == silent
    assert (await cut)["reason"] == "preempted"


async def test_a_say_the_tts_fails_on_midway_skips_the_rest_and_says_it_was_interrupted(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    failing = EngineConfig("tinyaisense_hub.speech.fake:FakeTts", {"fail_on": "boom"})
    hub = await start_hub(speech=replace(SPEECH, engines={"fake": failing}))
    sim = simulator(hub.device_address)
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        broken, after = await asyncio.gather(
            agent.say("kitchen/speaker", "One two three four, boom. Never heard."),
            agent.say("kitchen/speaker", "Bye."),
        )

    assert broken == {
        "reason": "drained",
        "played_ms": 190,
        "interrupted": True,
        "heard_text": "One two three four,",
    }
    assert after["reason"] == "drained"
    assert fake.decode(kitchen.speaker.pcm()).text == "One two three four,Bye."


async def test_a_tts_that_names_no_languages_speaks_any(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    any_language = EngineConfig("tinyaisense_hub.speech.fake:FakeTts", {"languages": []})
    hub = await start_hub(speech=replace(SPEECH, engines={"fake": any_language}))
    sim = simulator(hub.device_address)
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await agent.say("kitchen/speaker", "Привет.")

    assert fake.decode(kitchen.speaker.pcm()).text == "Привет."
