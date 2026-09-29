"""Raw mic streams from a device to an agent, and exclusive groups:
docs/protocol/agent.md#streams."""

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import pytest
import tinyaisense
import websockets
from tinyaisense_hub import Hub, OwnerClient
from tinyaisense_sim import SimDevice, Simulator, sources
from tinyaisense_sim.pytest_plugin import SimulatorFactory
from tinyaisense_wire import (
    HubAudio,
    JsonObject,
    StreamFrame,
    decode_hub_audio,
    encode_datagram,
)
from websockets.asyncio.client import ClientConnection

from .conftest import FAST_TIMING, StartHub

ECHO = "m5stack-atom-echo"
TOKEN = "agent-token"
"""The `start_hub` fixture's agent token."""
FRAME = 320
"""10 ms of 16 kHz mono s16le."""
HELLO = {"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {"token": TOKEN}}


def request(request_id: int, method: str, **params: Any) -> JsonObject:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


async def feed_faster_than_real_time(device: SimDevice, pcm: bytes) -> None:
    """Have the mic capture `pcm` 100 ms at a time, each as soon as the hub has taken in the
    last."""
    for at in range(0, len(pcm), 10 * FRAME):
        device.mic.feed(pcm[at : at + 10 * FRAME], burst=True)
        await asyncio.sleep(0.005)


async def slow_agent(hub: Hub) -> ClientConnection:
    """An agent's link that reads only when the test does, with a socket that holds next to
    nothing: the hub's audio backs up as soon as the test stops reading."""
    url = urlsplit(hub.agent_url)
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.connect((url.hostname, url.port))
    ws = await websockets.connect(hub.agent_url, sock=sock, max_queue=1, ping_interval=None)
    await ws.send(json.dumps(HELLO))
    await ws.recv()
    return ws


async def paired(sim: Simulator, owner: OwnerClient, name: str = "kitchen") -> SimDevice:
    device = await sim.add(ECHO, name)
    await owner.approve(device.device_id, name)
    await device.wait_for(lambda: device.state == "online")
    return device


async def take(frames: AsyncIterator[HubAudio], count: int) -> list[HubAudio]:
    return [await anext(frames) for _ in range(count)]


def contiguous(frames: list[HubAudio]) -> bytes:
    """The PCM of `frames`, once checked to be a timeline from 0 with no gap."""
    assert [f.index for f in frames] == list(range(len(frames)))
    return b"".join(f.pcm for f in frames)


def at_frame(timeline: bytes, audio: bytes) -> int:
    """Where `audio` starts in `timeline`, in frames: it's captured on a frame boundary."""
    at = timeline.find(audio)
    assert at >= 0, "the audio isn't in the timeline"
    assert at % FRAME == 0
    return at // FRAME


async def test_the_agent_gets_what_the_mic_hears_as_a_timeline_of_10_ms_frames(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    tone = sources.tone(300)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/mic")
        frames = agent.audio(handle)
        kitchen.mic.feed(tone, burst=True)
        received = await take(frames, 30)

        ended = await agent.stream_stop(handle)
        received += [f async for f in frames]

    assert ended["reason"] == "stopped"
    timeline = contiguous(received)
    assert all(f.handle == handle for f in received)
    at_frame(timeline, tone)
    assert ended["stats"] == {"frames": len(received), "lost": 0, "dropped": 0, "skipped": 0}


def with_silence(pcm: bytes, frames: set[int]) -> bytes:
    """`pcm` with these 10 ms frames zeroed, as the hub fills what it lost."""
    out = bytearray(pcm)
    for n in frames:
        out[n * FRAME : (n + 1) * FRAME] = bytes(FRAME)
    return bytes(out)


async def record(agent: tinyaisense.Session, handle: int, count: int) -> tuple[bytes, JsonObject]:
    """Take `count` frames of the stream, stop it, and take the rest. Returns the timeline and
    the end."""
    frames = agent.audio(handle)
    received = await take(frames, count)
    ended = await agent.stream_stop(handle)
    received += [f async for f in frames]
    assert ended["stats"]["frames"] == len(received)
    return contiguous(received), ended


async def test_audio_lost_on_the_way_up_is_zero_filled_and_counted_lost(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_up.drop(None, {3, 4, 7})
    tone = sources.tone(100)
    kitchen.mic.feed(tone)  # what it hears from its first frame on
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/mic")

        timeline, ended = await record(agent, handle, 10)

    assert timeline.startswith(with_silence(tone, {3, 4, 7}))
    assert ended["stats"] | {"frames": 0} == {"frames": 0, "lost": 3, "dropped": 0, "skipped": 0}


async def test_frames_that_arrive_twice_are_delivered_once(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_up.duplicate = 1.0
    tone = sources.tone(100)
    kitchen.mic.feed(tone)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/mic")

        timeline, ended = await record(agent, handle, 10)

    assert timeline.startswith(tone)
    assert ended["stats"]["lost"] == 0


async def test_malformed_datagrams_are_dropped_and_their_frames_zero_filled(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_up.drop(None, {2, 3, 4})
    tone = sources.tone(100)
    kitchen.mic.feed(tone)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/mic")
        assert kitchen.conn_id is not None
        assert kitchen.mic.stream is not None
        # In the dropped frames' place: an odd byte count, a short frame that isn't the
        # last, and an oversized one.
        forged = {2: bytes(FRAME - 1), 3: bytes(100), 4: bytes(FRAME + 2)}
        loop = asyncio.get_running_loop()
        udp, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
        )
        for seq, payload in forged.items():
            frame = StreamFrame(kitchen.conn_id, kitchen.mic.stream, seq, 0, payload)
            udp.sendto(encode_datagram(frame), ("127.0.0.1", hub.udp_port))
        udp.close()

        timeline, ended = await record(agent, handle, 10)

    assert timeline.startswith(with_silence(tone, {2, 3, 4}))
    assert ended["stats"]["lost"] == 3


async def test_datagrams_from_elsewhere_or_of_another_connection_are_dropped(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_up.drop(None, {2, 3})
    tone = sources.tone(100)
    kitchen.mic.feed(tone)
    loop = asyncio.get_running_loop()
    try:
        elsewhere, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("127.0.0.2", 0)
        )
    except OSError:
        pytest.skip("no 127.0.0.2 on this machine")
    here, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
    )
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/mic")
        assert kitchen.conn_id is not None
        assert kitchen.mic.stream is not None
        hub_udp = ("127.0.0.1", hub.udp_port)
        # Whole, well-formed frames in the dropped frames' place: of another Connection, and
        # from an IP that isn't the device's.
        other = StreamFrame(kitchen.conn_id + 1, kitchen.mic.stream, 2, 0, sources.tone(10))
        here.sendto(encode_datagram(other), hub_udp)
        spoofed = StreamFrame(kitchen.conn_id, kitchen.mic.stream, 3, 0, sources.tone(10))
        elsewhere.sendto(encode_datagram(spoofed), hub_udp)

        timeline, ended = await record(agent, handle, 10)
    here.close()
    elsewhere.close()

    assert timeline.startswith(with_silence(tone, {2, 3}))
    assert ended["stats"]["lost"] == 2


async def test_the_stop_waits_for_frames_still_on_their_way(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_up.delay_ms = 20  # the stop reply overtakes the last frames
    tone = sources.tone(1000)
    kitchen.mic.feed(tone)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/mic")

        timeline, ended = await record(agent, handle, 10)

    assert tone.startswith(timeline)
    assert ended["stats"]["lost"] == 0


async def test_the_stop_waits_30_ms_for_the_tail_then_zero_fills_it(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_up.delay_ms = 80
    tone = sources.tone(1000)
    kitchen.mic.feed(tone)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/mic")

        timeline, ended = await record(agent, handle, 10)

    lost = ended["stats"]["lost"]
    assert 0 < lost < 8
    frames = len(timeline) // FRAME
    assert timeline == with_silence(tone[: len(timeline)], set(range(frames - lost, frames)))


async def test_frames_the_device_dropped_leave_a_zero_filled_gap_counted_dropped(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    tone = sources.tone(500)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/mic")
        kitchen.faults.mic_tx_stall = True
        kitchen.mic.feed(tone, burst=True)  # its queue holds the newest 200 ms
        kitchen.faults.mic_tx_stall = False

        timeline, ended = await record(agent, handle, 40)

    stats = ended["stats"]
    assert stats["dropped"] >= 30
    assert stats["lost"] == 0
    end = at_frame(timeline, tone[30 * FRAME :])
    assert timeline[(end - stats["dropped"]) * FRAME : end * FRAME] == bytes(
        stats["dropped"] * FRAME
    )


async def test_starting_the_mic_preempts_its_exclusive_peer_the_speaker(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        speaker = await agent.stream_start("kitchen/speaker")
        await agent.send_audio(speaker, sources.tone(2000))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 5)

        mic = await agent.stream_start("kitchen/mic")

        assert kitchen.speaker.stream is None
        assert kitchen.mic.stream is not None
        methods = [(await anext(notifications)).params for _ in range(3)]
        await agent.stream_stop(mic)
    assert [(p["handle"], p.get("reason")) for p in methods] == [
        (speaker, None),
        (speaker, "preempted"),
        (mic, None),
    ]
    assert methods[1]["stats"]["played_ms"] > 0


async def test_starting_the_speaker_preempts_the_mic_after_its_last_frames(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.faults.udp_up.delay_ms = 20
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        mic = await agent.stream_start("kitchen/mic")
        frames = agent.audio(mic)
        received = await take(frames, 5)

        speaker = await agent.stream_start("kitchen/speaker")

        received += [f async for f in frames]
        assert kitchen.speaker.stream is not None
        methods = [(await anext(notifications)).params for _ in range(3)]
    assert [(p["handle"], p.get("reason")) for p in methods] == [
        (mic, None),
        (mic, "preempted"),
        (speaker, None),
    ]
    contiguous(received)
    assert methods[1]["stats"] == {
        "frames": len(received),
        "lost": 0,
        "dropped": 0,
        "skipped": 0,
    }


async def test_a_second_mic_stream_preempts_the_first(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    tone = sources.tone(300)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        old = await agent.stream_start("kitchen/mic")
        old_frames = agent.audio(old)
        await take(old_frames, 2)

        new = await agent.stream_start("kitchen/mic")
        kitchen.mic.feed(tone, burst=True)

        contiguous([f async for f in old_frames])
        timeline, ended = await record(agent, new, 30)
        methods = [(await anext(notifications)).params for _ in range(3)]

    assert [(p["handle"], p.get("reason")) for p in methods] == [
        (old, None),
        (old, "preempted"),
        (new, None),
    ]
    assert ended["reason"] == "stopped"
    at_frame(timeline, tone)


async def test_a_device_that_drops_ends_its_mic_stream_disconnected(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        handle = await agent.stream_start("kitchen/mic")
        frames = agent.audio(handle)
        await take(frames, 3)

        kitchen.faults.rst()

        [f async for f in frames]  # the frames end
        assert (await anext(notifications)).method == "stream.started"
        ended = await anext(notifications)
        assert ended.params["handle"] == handle
        assert ended.params["reason"] == "disconnected"
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.stream_stop(handle)
        assert refused.value.code == "disconnected"


async def test_a_session_that_ends_stops_its_mic_streams(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as old:
        notifications = old.notifications()
        handle = await old.stream_start("kitchen/mic")
        frames = old.audio(handle)
        await take(frames, 3)

        async with tinyaisense.connect(hub.agent_url, TOKEN, takeover=True):
            assert kitchen.mic.stream is None
            [f async for f in frames]
            assert (await anext(notifications)).method == "stream.started"
            ended = await anext(notifications)
            assert ended.params["reason"] == "disconnected"
            assert (await anext(notifications)).method == "session.ended"


async def test_a_mic_stream_cant_be_drained_only_stopped(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/mic")

        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.stream_drain(handle)

        assert refused.value.code == "unsupported"
        assert kitchen.mic.stream is not None


async def test_audio_sent_to_a_mic_stream_is_dropped_and_the_link_stays_up(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/mic")

        await agent.send_audio(handle, sources.tone(50))

        assert len(await agent.devices()) == 1
        assert kitchen.speaker.stream is None


async def test_the_device_shows_listening_while_its_mic_streams(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await agent.command("kitchen/led", "set", {"color": "#ff0000"})

        handle = await agent.stream_start("kitchen/mic")
        assert kitchen.status == "listening"

        await agent.stream_stop(handle)
        assert kitchen.status == "claim"


async def test_an_agent_that_reads_too_slowly_skips_the_oldest_audio_as_gaps_in_its_index(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    hub = await start_hub(timing=replace(FAST_TIMING, agent_audio_ms=200))
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(simulator(hub.device_address), owner)
    async with await slow_agent(hub) as ws:
        await ws.send(json.dumps(request(2, "stream.start", address="kitchen/mic")))
        assert json.loads(await ws.recv())["method"] == "stream.started"
        handle = json.loads(await ws.recv())["result"]["handle"]

        await feed_faster_than_real_time(kitchen, sources.tone(5000))
        await ws.send(json.dumps(request(3, "stream.stop", handle=handle)))

        indices: list[int] = []
        replies: list[JsonObject] = []
        async for message in ws:
            if isinstance(message, bytes):
                indices.append(decode_hub_audio(message).index)
            elif "id" in (reply := json.loads(message)):
                replies.append(reply)
                break

    stats = replies[0]["result"]["stats"]
    skipped = set(range(indices[-1] + 1)) - set(indices)
    assert indices == sorted(indices)
    assert stats["skipped"] == len(skipped) > 0
    assert stats["frames"] == len(indices)


async def test_json_goes_out_ahead_of_the_audio_waiting_for_a_slow_agent(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    hub = await start_hub(timing=replace(FAST_TIMING, agent_audio_ms=200))
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(simulator(hub.device_address), owner)
    async with await slow_agent(hub) as ws:
        await ws.send(json.dumps(request(2, "stream.start", address="kitchen/mic")))
        assert json.loads(await ws.recv())["method"] == "stream.started"
        await ws.recv()
        await feed_faster_than_real_time(kitchen, sources.tone(5000))
        kitchen.faults.mic_tx_stall = True  # then nothing more

        await ws.send(json.dumps(request(3, "devices")))

        while not isinstance(message := await ws.recv(), str):
            pass
        assert json.loads(message)["id"] == 3
        behind = 0
        with contextlib.suppress(TimeoutError):
            while True:
                assert isinstance(await asyncio.wait_for(ws.recv(), 0.2), bytes)
                behind += 1
    assert behind == 20  # the 200 ms the hub held


@pytest.mark.parametrize(
    ("host", "device_host"),
    [("::1", "::1"), (None, "::1"), (None, "127.0.0.1")],
    ids=["ipv6", "dual-stack-ipv6", "dual-stack-ipv4"],
)
async def test_mic_audio_reaches_the_agent_over_either_ip_family(
    start_hub: StartHub, simulator: SimulatorFactory, host: str | None, device_host: str
) -> None:
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    hub = await start_hub(host=host)
    sim = simulator((device_host, hub.device_address[1]))
    tone = sources.tone(100)
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(sim, owner)
    kitchen.mic.feed(tone)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/mic")

        timeline, ended = await record(agent, handle, 10)

    assert timeline.startswith(tone)
    assert ended["stats"]["lost"] == 0


async def test_a_streams_end_waits_for_its_last_frames_but_other_json_doesnt(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    hub = await start_hub(timing=replace(FAST_TIMING, agent_audio_ms=200))
    async with OwnerClient(hub.owner_socket) as owner:
        kitchen = await paired(simulator(hub.device_address), owner)
    async with await slow_agent(hub) as ws:
        await ws.send(json.dumps(request(2, "stream.start", address="kitchen/mic")))
        assert json.loads(await ws.recv())["method"] == "stream.started"
        await ws.recv()
        await feed_faster_than_real_time(kitchen, sources.tone(5000))
        kitchen.faults.mic_tx_stall = True  # then nothing more

        await ws.send(json.dumps(request(3, "stream.stop", handle=1)))
        await asyncio.sleep(0.1)  # it has ended, with frames still waiting for the socket
        await ws.send(json.dumps(request(4, "devices")))

        order: list[str] = []
        with contextlib.suppress(TimeoutError):
            while True:
                message = await asyncio.wait_for(ws.recv(), 0.2)
                if isinstance(message, bytes):
                    order.append("frame")
                else:
                    reply = json.loads(message)
                    order.append(reply.get("method") or f"reply {reply['id']}")
    last_frame = len(order) - 1 - order[::-1].index("frame")
    assert order.index("stream.ended") > last_frame
    assert order.index("reply 4") < last_frame
