"""What the owner debugs a bad conversation with: the always-on log lines, and the opt-in
recording dir."""

import asyncio
import contextlib
import json
import logging
import time
import wave
from pathlib import Path
from typing import Any

import pytest
import tinyaisense
from tinyaisense_hub import Hub, OwnerClient
from tinyaisense_hub.speech import fake
from tinyaisense_sim import SimDevice, Simulator, sources
from tinyaisense_sim.pytest_plugin import SimulatorFactory

from .conftest import StartHub
from .test_listen import SPEECH, utterance
from .test_mcp import call, mcp

ECHO = "m5stack-atom-echo"
TOKEN = "agent-token"
"""The `start_hub` fixture's agent token."""


async def paired(sim: Simulator, owner: OwnerClient, name: str = "kitchen") -> SimDevice:
    device = await sim.add(ECHO, name)
    await owner.approve(device.device_id, name)
    await device.wait_for(lambda: device.state == "online")
    return device


def logged(caplog: pytest.LogCaptureFixture, type: str) -> list[dict[str, Any]]:
    """The fields of each log line of `type`, in order."""
    lines = [getattr(record, "fields", {}) for record in caplog.records]
    return [fields for fields in lines if fields.get("type") == type]


async def test_a_mic_stream_logs_its_counters_gaps_and_mouth_to_hub_latency_as_it_ends(
    hub: Hub, sim: Simulator, owner: OwnerClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "tinyaisense_hub")
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_up.drop(None, {3, 4, 7})
    kitchen.faults.udp_up.delay_ms = 20
    kitchen.faults.udp_up.duplicate = 1.0
    kitchen.mic.feed(sources.tone(300))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/mic")
        frames = agent.audio(handle)
        for _ in range(20):
            await anext(frames)

        ended = await agent.stream_stop(handle)

    [line] = logged(caplog, "stream.ended")
    assert (line["address"], line["kind"], line["reason"]) == ("kitchen/mic", "mic", "stopped")
    assert line["stats"] == ended["stats"]
    assert line["gaps"] == 2
    assert line["counters"]["zero_filled"] == 3
    assert line["counters"]["late"] > 0  # the duplicates
    assert line["counters"]["rejected"] == {}
    # Each frame leaves 10 ms after its first sample, then waits out the 20 ms delay. Only
    # the frames taken into the timeline count: not the duplicates.
    latency = line["mouth_to_hub_ms"]
    assert 28 <= latency["p50"] <= latency["p99"] < 200
    assert latency["count"] == ended["stats"]["frames"] - 3


async def test_a_speaker_stream_logs_its_counters_and_the_gaps_the_device_played_as_it_ends(
    hub: Hub, sim: Simulator, owner: OwnerClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "tinyaisense_hub")
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(handle, sources.tone(300))

        ended = await agent.stream_drain(handle)

    [line] = logged(caplog, "stream.ended")
    assert (line["address"], line["kind"], line["reason"]) == (
        "kitchen/speaker",
        "speaker",
        "drained",
    )
    assert line["stats"] == ended["stats"]
    assert line["counters"] == {"sent": 30}
    assert line["gaps"] == 0


async def test_a_speaker_stream_whose_device_drops_logs_no_gaps_it_cant_know(
    hub: Hub, sim: Simulator, owner: OwnerClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "tinyaisense_hub")
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(handle, sources.tone(300))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 5)

        kitchen.faults.rst()

        async for notification in agent.notifications():
            if notification.method == "stream.ended":
                break

    [line] = logged(caplog, "stream.ended")
    assert line["reason"] == "disconnected"
    assert line["gaps"] is None  # its stats are the hub's estimate


def stall(ms: int) -> None:
    """Hold up the loop, as a callback that runs too long would."""
    time.sleep(ms / 1000)


async def test_the_hub_warns_when_its_loop_lags_over_5_ms(
    hub: Hub, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "tinyaisense_hub")
    await asyncio.sleep(0.05)

    stall(30)
    await asyncio.sleep(0.05)

    [line] = logged(caplog, "loop.lag")
    assert line["count"] == 1
    assert 20 <= line["max_ms"] < 100


async def test_lags_within_1_s_of_a_warning_come_as_one_line_with_their_count_and_max(
    hub: Hub, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "tinyaisense_hub")
    await asyncio.sleep(0.05)
    stall(30)
    await asyncio.sleep(0.05)

    stall(30)
    await asyncio.sleep(0.05)
    stall(60)
    await asyncio.sleep(0.05)
    assert len(logged(caplog, "loop.lag")) == 1
    await asyncio.sleep(1)

    [_, line] = logged(caplog, "loop.lag")
    assert line["count"] == 2
    assert 45 <= line["max_ms"] < 150


# The recording dir.


async def recorded(root: Path) -> Path:
    """The dir of the one Session recorded under `root`, once the Session has ended."""
    async with asyncio.timeout(5):
        while True:
            if (session := ended(root)) is not None:
                return session
            await asyncio.sleep(0.01)


def ended(root: Path) -> Path | None:
    """The dir of the one Session recorded under `root`, if it has ended."""
    if not root.exists() or not (dirs := list(root.iterdir())):
        return None
    [session] = dirs
    return session if any(r["type"] == "session.ended" for r in trace(session)) else None


def trace(session: Path) -> list[dict[str, Any]]:
    lines = (session / "trace.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def wav(path: Path) -> bytes:
    """The PCM of a WAV, once checked to be 16 kHz mono s16le."""
    with wave.open(str(path), "rb") as f:
        assert (f.getframerate(), f.getnchannels(), f.getsampwidth()) == (16000, 1, 2)
        return f.readframes(f.getnframes())


async def test_the_record_dir_keeps_a_wav_of_each_stream_as_the_mic_heard_it_and_the_speaker_got_it(
    start_hub: StartHub, simulator: SimulatorFactory, tmp_path: Path
) -> None:
    hub = await start_hub(record_dir=tmp_path / "rec")
    sim = simulator(hub.device_address)
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(sim, owner)
    kitchen.faults.udp_up.drop(None, {3})
    kitchen.mic.feed(sources.tone(200, hz=300), burst=True)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        mic = await agent.stream_start("kitchen/mic")
        frames = agent.audio(mic)
        heard = [await anext(frames) for _ in range(15)]
        await agent.stream_stop(mic)
        heard += [f async for f in frames]
        speaker = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(speaker, sources.tone(300))
        await agent.stream_drain(speaker)

    session = await recorded(tmp_path / "rec")
    assert sorted(p.name for p in session.glob("*.wav")) == [
        "001-kitchen-mic.wav",
        "002-kitchen-speaker.wav",
    ]
    assert wav(session / "001-kitchen-mic.wav") == b"".join(f.pcm for f in heard)
    assert wav(session / "002-kitchen-speaker.wav") == kitchen.speaker.pcm()


async def test_a_wav_plays_up_to_about_its_last_second_while_its_stream_still_runs(
    start_hub: StartHub, simulator: SimulatorFactory, tmp_path: Path
) -> None:
    hub = await start_hub(record_dir=tmp_path / "rec")
    sim = simulator(hub.device_address)
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(sim, owner)
    kitchen.mic.feed(sources.tone(1500, hz=300))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        mic = await agent.stream_start("kitchen/mic")
        frames = agent.audio(mic)
        heard = b"".join([(await anext(frames)).pcm for _ in range(150)])

        [session] = (tmp_path / "rec").iterdir()
        pcm = wav(session / "001-kitchen-mic.wav")
        assert len(pcm) >= 100 * 320  # 100 frames: 1 s
        assert heard.startswith(pcm)


async def test_the_trace_holds_the_agents_calls_and_replies_its_notifications_and_the_streams(
    start_hub: StartHub, simulator: SimulatorFactory, tmp_path: Path
) -> None:
    hub = await start_hub(record_dir=tmp_path / "rec")
    sim = simulator(hub.device_address)
    async with OwnerClient(hub.owner_socket) as owner:
        await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        speaker = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(speaker, sources.tone(100))
        ended = await agent.stream_drain(speaker)
        with pytest.raises(tinyaisense.ApiError):
            await agent.stream_stop(speaker)
        with pytest.raises(tinyaisense.ApiError):
            await agent.call("stream.start", adress="kitchen/speaker")
        with pytest.raises(tinyaisense.ApiError):
            await agent.call("stream.pause", handle=speaker)

    records = trace(await recorded(tmp_path / "rec"))
    ts = [r["t"] for r in records]
    assert ts == sorted(ts)
    assert (records[0]["type"], records[0]["session"]) == ("session.started", agent.id)
    assert (records[0]["holder"], records[0]["utterances"]["transcript"]) == ("agent_api", True)
    assert (records[-1]["type"], records[-1]["reason"]) == ("session.ended", "closed")
    calls = {r["call"]: r for r in records if r["type"] == "call"}
    replies = {r["call"]: r for r in records if r["type"] == "reply"}
    assert [(c["via"], c["method"], c["params"]) for c in calls.values()] == [
        ("agent_api", "stream.start", {"address": "kitchen/speaker"}),
        ("agent_api", "stream.drain", {"handle": speaker}),
        ("agent_api", "stream.stop", {"handle": speaker}),
        ("agent_api", "stream.start", {"adress": "kitchen/speaker"}),
        ("agent_api", "stream.pause", {"handle": speaker}),
    ]
    assert [r.get("result") for r in replies.values()] == [
        {"handle": speaker},
        ended,
        None,
        None,
        None,
    ]
    errors = [r["error"]["code"] for r in replies.values() if "error" in r]
    assert errors == ["disconnected", "invalid_args", "unsupported"]
    notified = [r["method"] for r in records if r["type"] == "notify"]
    assert notified == ["stream.started", "stream.ended"]
    [started] = [r for r in records if r["type"] == "stream.started"]
    assert started | {"t": 0} == {
        "t": 0,
        "type": "stream.started",
        "file": "001-kitchen-speaker.wav",
        "address": "kitchen/speaker",
        "kind": "speaker",
        "stream": started["stream"],
        "handle": speaker,
    }
    [stream_ended] = [r for r in records if r["type"] == "stream.ended"]
    assert stream_ended["file"] == "001-kitchen-speaker.wav"
    assert (stream_ended["reason"], stream_ended["stats"]) == ("drained", ended["stats"])
    assert stream_ended["counters"] == {"sent": 10}


async def test_a_call_made_while_its_session_ends_gets_its_reply_in_the_trace(
    start_hub: StartHub, simulator: SimulatorFactory, tmp_path: Path
) -> None:
    hub = await start_hub(record_dir=tmp_path / "rec")
    sim = simulator(hub.device_address)
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as old:
        kitchen.faults.delay_replies_ms = 300  # the Session's end waits on them
        async with contextlib.AsyncExitStack() as new:
            taking = asyncio.ensure_future(
                new.enter_async_context(tinyaisense.connect(hub.agent_url, TOKEN, takeover=True))
            )
            await kitchen.wait_for(lambda: kitchen.status == "no_agent")
            with pytest.raises((tinyaisense.ApiError, tinyaisense.SessionEnded)):
                await old.command("kitchen/led", "off")
            await taking

    [ended] = [d for d in (tmp_path / "rec").iterdir() if old.id in d.name]
    records = trace(ended)
    [call] = [r for r in records if r["type"] == "call"]
    [reply] = [r for r in records if r["type"] == "reply"]
    assert (call["method"], reply["call"]) == ("command", call["call"])
    assert reply["error"]["code"] == "disconnected"


async def test_a_call_cut_off_as_its_link_closes_is_traced_as_cancelled(
    start_hub: StartHub, simulator: SimulatorFactory, tmp_path: Path
) -> None:
    hub = await start_hub(record_dir=tmp_path / "rec", speech=SPEECH)
    sim = simulator(hub.device_address)
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        listening = asyncio.ensure_future(agent.listen("kitchen/mic"))
        await kitchen.wait_for(lambda: kitchen.mic.stream is not None)
    with contextlib.suppress(tinyaisense.ApiError, tinyaisense.SessionEnded):
        await listening

    records = trace(await recorded(tmp_path / "rec"))
    [reply] = [r for r in records if r["type"] == "reply"]
    assert (reply["call"], reply["cancelled"]) == (1, True)
    assert records[-1]["type"] == "session.ended"


async def test_the_trace_holds_the_mcp_tool_calls_of_a_session_and_their_replies(
    start_hub: StartHub, simulator: SimulatorFactory, tmp_path: Path
) -> None:
    hub = await start_hub(record_dir=tmp_path / "rec")
    sim = simulator(hub.device_address)
    async with OwnerClient(hub.owner_socket) as owner:
        await paired(sim, owner)
    async with mcp(hub, "2025-11-25") as client:  # leaving it ends its Session
        await call(client, "set_reflex", {"device": "kitchen", "armed": False})
        await call(
            client, "command", {"address": "attic/led", "name": "off", "args": {}}, fails=True
        )

    records = trace(await recorded(tmp_path / "rec"))
    assert records[0]["holder"] == "mcp"
    calls = [(r["via"], r["method"], r["params"]) for r in records if r["type"] == "call"]
    assert calls == [
        ("mcp", "set_reflex", {"device": "kitchen", "armed": False}),
        ("mcp", "command", {"address": "attic/led", "name": "off", "args": {}}),
    ]
    first, second = [r for r in records if r["type"] == "reply"]
    assert (first["call"], first["result"]) == (1, {})
    assert (second["call"], second["error"]["code"]) == (2, "unknown_address")


async def test_the_streams_the_hub_runs_for_a_say_and_push_to_talk_are_recorded_too(
    start_hub: StartHub, simulator: SimulatorFactory, tmp_path: Path
) -> None:
    hub = await start_hub(record_dir=tmp_path / "rec", speech=SPEECH)
    sim = simulator(hub.device_address)
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(sim, owner)
    phrase = fake.encode("Turn on the lights.")
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await kitchen.wait_for(lambda: kitchen.armed)
        await agent.say("kitchen/speaker", "Hello.")
        await kitchen.talk(phrase)
        await utterance(agent)

    session = await recorded(tmp_path / "rec")
    assert wav(session / "001-kitchen-speaker.wav") == kitchen.speaker.pcm()
    assert phrase in wav(session / "002-kitchen-mic.wav")
    started = [r for r in trace(session) if r["type"] == "stream.started"]
    assert [(r["file"], r["handle"]) for r in started] == [
        ("001-kitchen-speaker.wav", None),
        ("002-kitchen-mic.wav", None),
    ]
