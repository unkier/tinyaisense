"""Raw speaker streams from an agent to a device: docs/protocol/agent.md#streams."""

import asyncio
import itertools
import json
import math
import socket
import struct
from collections.abc import Awaitable, Callable
from dataclasses import replace

import pytest
import tinyaisense
import websockets
from tinyaisense_hub import Hub, OwnerClient, Timing
from tinyaisense_hub.speaker import FORMAT
from tinyaisense_sim import GilbertElliott, SimDevice, Simulator
from tinyaisense_sim.pytest_plugin import SimulatorFactory
from tinyaisense_wire import (
    Command,
    Event,
    Hello,
    JsonObject,
    Reply,
    StreamFrame,
    Welcome,
    encode_datagram,
)

from .conftest import FAST_TIMING
from .raw_device import RawDevice, echo_manifest

ECHO = "m5stack-atom-echo"
TOKEN = "agent-token"
"""The `start_hub` fixture's agent token."""
HELLO = {"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {"token": TOKEN}}

StartHub = Callable[..., Awaitable[Hub]]


async def paired(sim: Simulator, owner: OwnerClient, name: str = "kitchen") -> SimDevice:
    device = await sim.add(ECHO, name)
    await owner.approve(device.device_id, name)
    await device.wait_for(lambda: device.state == "online")
    return device


def tone(ms: int, hz: int = 440) -> bytes:
    """`ms` of a sine at 16 kHz, s16le mono."""
    samples = (round(12000 * math.sin(2 * math.pi * hz * n / 16000)) for n in range(ms * 16))
    return b"".join(struct.pack("<h", s) for s in samples)


async def test_the_device_plays_what_the_agent_streams_and_drain_replies_when_it_has_played(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    audio = tone(300)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(handle, audio)

        ended = await agent.stream_drain(handle)

    assert ended["reason"] == "drained"
    assert ended["stats"]["played_ms"] == 300
    assert ended["stats"]["lost"] == 0
    assert kitchen.speaker.pcm() == audio


async def test_stream_started_and_stream_ended_bracket_a_stream(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        handle = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(handle, tone(50))
        ended = await agent.stream_drain(handle)

        started = await anext(notifications)
        assert started.method == "stream.started"
        assert started.params | {"t": 0} == {
            "handle": handle,
            "address": "kitchen/speaker",
            "rate": 16000,
            "t": 0,
        }
        last = await anext(notifications)
        assert last.method == "stream.ended"
        assert last.params | {"t": 0} == {"handle": handle, **ended, "t": 0}


async def test_draining_a_stream_that_carried_no_audio_ends_it_at_once(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/speaker")

        ended = await agent.stream_drain(handle)

    assert ended == {
        "reason": "drained",
        "stats": {"played_ms": 0, "lost": 0, "underruns": 0, "underrun_ms": 0},
    }
    assert kitchen.speaker.pcm() == b""


async def test_stop_cuts_the_stream_and_replies_with_what_was_heard(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(handle, tone(2000))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 10)

        ended = await agent.stream_stop(handle)

    assert ended["reason"] == "stopped"
    played_ms = ended["stats"]["played_ms"]
    assert 50 <= played_ms < 2000
    assert kitchen.speaker.stream is None
    # What the device heard, to the sample: not what was still in its DMA.
    assert len(kitchen.speaker.pcm()) == played_ms * 32


async def test_a_new_stream_on_a_busy_speaker_preempts_the_old_one(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        old = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(old, tone(2000))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 5)

        new = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(old, tone(100))  # dropped: its stream has ended
        await agent.send_audio(new, tone(100, hz=880))
        ended = await agent.stream_drain(new)

        methods = [(await anext(notifications)).params for _ in range(4)]
    assert [(p["handle"], p.get("reason")) for p in methods] == [
        (old, None),
        (old, "preempted"),
        (new, None),
        (new, "drained"),
    ]
    assert methods[1]["stats"]["played_ms"] > 0
    assert ended["stats"]["played_ms"] == 100
    assert kitchen.speaker.pcm().endswith(tone(100, hz=880))


async def test_stop_silences_the_stream_at_once_even_behind_a_slow_command(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(handle, tone(3000))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 5)
        kitchen.faults.delay_replies_ms = 300
        slow = asyncio.create_task(agent.command("kitchen/led", "off"))
        await kitchen.wait_for(lambda: kitchen.status == "claim")  # its reply is held

        ended = await agent.stream_stop(handle)
        await slow

    # The audio stopped before the `stop` got its turn: the device ran dry meanwhile.
    assert ended["reason"] == "stopped"
    assert ended["stats"]["underruns"] == 1


async def test_a_drain_the_device_fails_stops_the_stream(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(handle, tone(100))
        kitchen.faults.fail_next()  # the drain

        ended = await agent.stream_drain(handle)

    assert ended["reason"] == "stopped"
    assert kitchen.speaker.stream is None


async def test_send_audio_refuses_pcm_that_isnt_whole_samples(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/speaker")

        with pytest.raises(ValueError, match="whole number"):
            await agent.send_audio(handle, bytes(41))


async def test_the_device_shows_speaking_while_its_speaker_plays(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/speaker")
        assert kitchen.status == "speaking"
        await agent.send_audio(handle, tone(50))

        await agent.stream_drain(handle)

        assert kitchen.status == "agent"


async def test_a_device_that_drops_ends_its_streams_disconnected(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        handle = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(handle, tone(2000))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 5)

        kitchen.faults.rst()

        assert (await anext(notifications)).method == "stream.started"
        ended = await anext(notifications)
        assert ended.method == "stream.ended"
        assert ended.params["reason"] == "disconnected"
        # The hub's estimate: what it sent, capped by the time since it started sending.
        assert 0 < ended.params["stats"]["played_ms"] < 2000
        assert (await anext(notifications)).method == "device.offline"
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.stream_drain(handle)
        assert refused.value.code == "disconnected"


async def test_a_session_that_ends_stops_its_streams(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as old:
        notifications = old.notifications()
        handle = await old.stream_start("kitchen/speaker")
        await old.send_audio(handle, tone(2000))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 5)

        async with tinyaisense.connect(hub.agent_url, TOKEN, takeover=True):
            assert kitchen.speaker.stream is None
            assert (await anext(notifications)).method == "stream.started"
            ended = await anext(notifications)
            assert ended.params["reason"] == "disconnected"
            # The device's own stats, from its `stop` reply.
            assert len(kitchen.speaker.pcm()) == ended.params["stats"]["played_ms"] * 32
            assert (await anext(notifications)).method == "session.ended"


async def test_an_agent_that_sends_too_far_ahead_overflows(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    hub = await start_hub(timing=replace(FAST_TIMING, speaker_buffer_ms=500))
    sim = simulator(hub.device_address)
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        handle = await agent.stream_start("kitchen/speaker")

        await agent.send_audio(handle, tone(1000))

        assert (await anext(notifications)).method == "stream.started"
        ended = await anext(notifications)
        assert ended.params["reason"] == "overflow"
        assert kitchen.speaker.stream is None


async def test_audio_on_an_ended_or_unknown_handle_is_dropped_and_the_link_stays_up(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/speaker")
        await agent.stream_stop(handle)

        await agent.send_audio(handle, tone(50))
        await agent.send_audio(999, tone(50))

        assert len(await agent.devices()) == 1


async def test_malformed_binary_frames_are_dropped_and_the_link_stays_up(hub: Hub) -> None:
    async with await websockets.connect(hub.agent_url, ping_interval=None) as ws:
        await ws.send(json.dumps(HELLO))
        await ws.recv()

        for frame in (b"", b"\x01", b"\x01\x00\x00"):  # truncated, then a partial sample
            await ws.send(frame)
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "devices"}))

        assert json.loads(await ws.recv()) == {"jsonrpc": "2.0", "id": 2, "result": {"devices": []}}


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"address": "kitchen/mic"}, "unsupported"),
        ({"address": "kitchen/led"}, "unsupported"),
        ({"address": "kitchen/speaker", "rate": 24000}, "unsupported"),
        ({"address": "kitchen/speaker", "rate": 8000}, "invalid_args"),
        ({"address": "kitchen/speaker", "prefill_ms": 201}, "invalid_args"),
        ({"address": "den/speaker"}, "unknown_address"),
    ],
)
async def test_stream_start_refuses_what_the_hub_cant_stream(
    hub: Hub, sim: Simulator, owner: OwnerClient, params: JsonObject, code: str
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.call("stream.start", **params)

    assert refused.value.code == code


async def test_stopping_a_handle_that_names_no_stream_is_disconnected(hub: Hub) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.stream_stop(7)

    assert refused.value.code == "disconnected"


# The commands, seen by a hand-rolled device that replies when the test says.

NEVER_DEAD = Timing(hello_timeout_ms=300, ping_interval_ms=100, dead_after_ms=60_000)
"""A raw device never answers pings."""
STATS = {"played_ms": 120, "lost": 1, "underruns": 0, "underrun_ms": 0}


async def command(device: RawDevice) -> Command:
    message = await device.receive()
    assert isinstance(message, Command)
    return message


async def online(hub: Hub) -> tuple[RawDevice, str]:
    """A raw Echo, welcomed and brought up. Returns it and its speaker's address."""
    device = await RawDevice.connect(hub)
    device.send(Hello(None, 5000, echo_manifest()))
    welcome = await device.receive()
    assert isinstance(welcome, Welcome)
    status = await command(device)
    device.send(Reply(status.id, {}))
    return device, f"{welcome.name}/speaker"


async def started(device: RawDevice, agent: tinyaisense.Session, speaker: str) -> int:
    """Open a stream on the raw device's speaker, which replies to `start`."""
    handle = asyncio.create_task(agent.stream_start(speaker))
    start = await command(device)
    device.send(Reply(start.id, {"format": start.args["format"]}))
    return await handle


async def test_a_restart_goes_out_as_stop_then_start_on_a_new_id_pipelined(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD)
    device, speaker = await online(hub)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await command(device)  # the Status: agent
        first = asyncio.create_task(agent.stream_start(speaker))
        start = await command(device)
        device.send(Reply(start.id, {"format": start.args["format"]}))
        old = await first

        second = asyncio.create_task(agent.stream_start(speaker))
        # Both go out before either reply is in.
        stop, restart = await command(device), await command(device)
        device.send(Reply(stop.id, STATS))
        device.send(Reply(restart.id, {"format": restart.args["format"]}))
        new = await second

        assert (start.cap, start.name, start.args) == (
            "speaker",
            "start",
            {"stream": 1, "format": FORMAT.to_json(), "prefill_ms": 40},
        )
        assert (stop.cap, stop.name, stop.args) == ("speaker", "stop", {})
        assert (restart.name, restart.args["stream"]) == ("start", 2)
        assert new != old
        with pytest.raises(tinyaisense.ApiError):
            await agent.stream_stop(old)  # it ended, preempted, with the device's stats


async def test_a_drained_event_ends_the_stream_with_the_devices_stats(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD)
    device, speaker = await online(hub)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await command(device)  # the Status: agent
        handle = await started(device, agent, speaker)

        ending = asyncio.create_task(agent.stream_drain(handle))
        drain = await command(device)
        device.send(Reply(drain.id, {}))
        # A `drained` for another stream id isn't this one's.
        device.send(Event(0, "speaker", "drained", 0, {"stream": 9} | STATS))
        device.send(Event(1, "speaker", "drained", 0, {"stream": 1} | STATS))

        assert (drain.name, drain.args) == ("drain", {"last_seq": None})
        assert await ending == {"reason": "drained", "stats": STATS}


# The network between hub and device, and what the device takes from it.


async def played(agent: tinyaisense.Session, audio: bytes) -> JsonObject:
    """Stream `audio` to the kitchen speaker to its end. Returns the stats."""
    handle = await agent.stream_start("kitchen/speaker")
    await agent.send_audio(handle, audio)
    ended = await agent.stream_drain(handle)
    assert ended["reason"] == "drained"
    return ended["stats"]


def silenced(audio: bytes, seqs: set[int]) -> bytes:
    """`audio` with the 10 ms frames `seqs` as silence."""
    frames = [audio[at : at + 320] for at in range(0, len(audio), 320)]
    return b"".join(bytes(320) if seq in seqs else f for seq, f in enumerate(frames))


async def test_the_device_drops_and_counts_datagrams_that_arent_from_its_connection(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        first = await agent.stream_start("kitchen/speaker")
        closed = kitchen.speaker.stream
        await agent.stream_stop(first)
        handle = await agent.stream_start("kitchen/speaker")
        live = kitchen.speaker.stream
        assert kitchen.conn_id is not None
        assert closed is not None
        assert live is not None

        def frame(stream: int, conn_id: int = kitchen.conn_id, pcm: bytes = bytes(320)) -> bytes:
            return encode_datagram(StreamFrame(conn_id, stream, 0, 0, pcm))

        with (
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as hub_ip,
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as elsewhere,
        ):
            hub_ip.bind(("127.0.0.1", 0))
            elsewhere.bind(("127.0.0.2", 0))
            to = ("127.0.0.1", kitchen.udp_port)
            elsewhere.sendto(frame(live), to)
            hub_ip.sendto(frame(live, conn_id=kitchen.conn_id + 1), to)
            hub_ip.sendto(b"junk", to)
            hub_ip.sendto(frame(live, pcm=bytes(319)), to)
            hub_ip.sendto(frame(100), to)
            hub_ip.sendto(frame(closed), to)  # a late frame of a closed stream: silent

            audio = tone(100)
            await agent.send_audio(handle, audio)
            ended = await agent.stream_drain(handle)

    assert kitchen.stats == {
        "bad_source": 1,
        "bad_conn_id": 1,
        "short_datagram": 1,
        "partial_sample": 1,
        "unknown_stream": 1,
    }
    assert ended["stats"]["lost"] == 0
    assert kitchen.speaker.pcm(live) == audio


async def test_bernoulli_loss_is_concealed_and_counted_as_lost(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_down.loss = 0.2
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        stats = await played(agent, tone(500))

    assert kitchen.faults.udp_down.dropped > 0
    assert stats["lost"] == kitchen.faults.udp_down.dropped
    assert stats["played_ms"] == 500


async def test_gilbert_elliott_loss_drops_bursts_and_counts_them_as_lost(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_down.bursts = GilbertElliott(p=0.1, r=0.3)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        stats = await played(agent, tone(1000))

    lost = [f.seq for f in kitchen.speaker.captured if f.lost]
    assert any(b - a == 1 for a, b in itertools.pairwise(lost))  # a burst
    assert stats["lost"] == kitchen.faults.udp_down.dropped == len(lost)
    assert stats["played_ms"] == 1000


async def test_a_delay_shifts_playout_and_loses_nothing(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_down.delay_ms = 40
    audio = tone(300)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/speaker")
        sent = asyncio.get_running_loop().time()
        await agent.send_audio(handle, audio)
        ended = await agent.stream_drain(handle)

    # The first frame arrives 40 ms after it went out, and is heard 30 ms after it plays,
    # give or take the loop clock's rounding.
    assert kitchen.speaker.captured[0].at >= sent + 0.065
    assert ended["stats"]["lost"] == 0
    assert ended["stats"]["played_ms"] == 300
    assert kitchen.speaker.pcm() == audio


async def test_jitter_under_the_hubs_lead_loses_nothing(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_down.jitter_ms = 30
    audio = tone(500)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        stats = await played(agent, audio)

    assert stats["lost"] == 0
    assert stats["played_ms"] == 500
    assert kitchen.speaker.pcm() == audio


async def test_duplicate_datagrams_are_played_once(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_down.duplicate = 1.0
    audio = tone(300)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        stats = await played(agent, audio)

    assert stats["lost"] == 0
    assert stats["played_ms"] == 300
    assert kitchen.speaker.pcm() == audio


async def test_dropped_frames_play_as_silence_and_count_as_lost(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_down.drop(None, {3, 4, 10})
    audio = tone(300)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        stats = await played(agent, audio)

    assert stats["lost"] == 3
    assert stats["played_ms"] == 300
    assert kitchen.speaker.pcm() == silenced(audio, {3, 4, 10})


async def test_a_blackout_underruns_and_skips_what_it_swallowed(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(handle, tone(600))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 10)

        kitchen.faults.udp_down.blackout(100)
        ended = await agent.stream_drain(handle)

    stats = ended["stats"]
    assert kitchen.faults.udp_down.dropped >= 5
    assert stats["lost"] == kitchen.faults.udp_down.dropped
    assert stats["underruns"] >= 1
    assert stats["played_ms"] == 600


async def test_a_reflex_cut_on_the_device_ends_the_stream_reflex(start_hub: StartHub) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD)
    device, speaker = await online(hub)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        await command(device)  # the Status: agent
        handle = await started(device, agent, speaker)

        device.send(Event(0, "speaker", "stopped", 0, {"stream": 1, "reason": "reflex"} | STATS))

        assert (await anext(notifications)).method == "stream.started"
        ended = await anext(notifications)
        assert ended.params | {"t": 0} == {
            "handle": handle,
            "reason": "reflex",
            "stats": STATS,
            "t": 0,
        }


async def test_a_start_that_times_out_is_stopped_in_case_the_device_starts_it_late(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(auto_approve=True, timing=NEVER_DEAD)
    device, speaker = await online(hub)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await command(device)  # the Status: agent
        starting = asyncio.create_task(agent.stream_start(speaker))
        start = await command(device)  # and no reply

        stop = await command(device)
        device.send(Reply(stop.id, STATS))

        assert (start.name, stop.name) == ("start", "stop")
        with pytest.raises(tinyaisense.ApiError) as refused:
            await starting
        assert refused.value.code == "timeout"
