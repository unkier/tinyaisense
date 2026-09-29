"""The mic Kind on a simulated device, driven by a hand-rolled hub: docs/protocol/kinds/mic.md."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from tinyaisense_sim import SimDevice, sources
from tinyaisense_sim.pytest_plugin import SimulatorFactory
from tinyaisense_wire import (
    Command,
    FailedReply,
    JsonObject,
    Ping,
    Pong,
    Reply,
    Welcome,
)

from .raw_hub import RawConnection, RawHub, RawUdp

ECHO = "m5stack-atom-echo"
CONN_ID = 7
FORMAT = {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": 10}


@dataclass
class Online:
    connection: RawConnection
    udp: RawUdp
    device: SimDevice
    next_id: int = 1

    async def command(
        self, cap: str | None, name: str, args: JsonObject | None = None
    ) -> Reply | FailedReply:
        command_id, self.next_id = self.next_id, self.next_id + 1
        self.connection.send(Command(command_id, cap, name, args or {}))
        while True:
            message = await self.connection.receive()
            if isinstance(message, Reply | FailedReply) and message.id == command_id:
                return message

    async def ok(self, cap: str | None, name: str, args: JsonObject | None = None) -> JsonObject:
        reply = await self.command(cap, name, args)
        assert isinstance(reply, Reply), reply
        return reply.result

    async def clock(self) -> int:
        """The device clock now, from a pong."""
        self.connection.send(Ping(0))
        while not isinstance(message := await self.connection.receive(), Pong):
            pass
        return message.ts


@pytest.fixture
async def online(raw_hub: RawHub, simulator: SimulatorFactory) -> AsyncIterator[Online]:
    """A simulated Echo, welcomed by the raw hub, with the hub's UDP socket."""
    udp = await RawUdp.open()
    sim = simulator(raw_hub.address)
    added = asyncio.create_task(sim.add(ECHO, "kitchen"))
    connection = await raw_hub.accept()
    connection.send(Welcome("kitchen", CONN_ID, udp.port))
    device = await added
    yield Online(connection, udp, device)
    udp.close()


def frames_of(pcm: bytes) -> list[bytes]:
    return [pcm[at : at + 320] for at in range(0, len(pcm), 320)]


async def test_a_started_mic_streams_what_it_is_fed_as_10_ms_frames_numbered_from_0(
    online: Online,
) -> None:
    tone = sources.tone(100)

    result = await online.ok("mic", "start", {"stream": 5, "format": FORMAT})
    online.device.mic.feed(tone, burst=True)

    frames = await online.udp.frames(10)
    assert result == {"format": FORMAT}
    assert [(f.conn_id, f.stream, f.seq) for f in frames] == [(CONN_ID, 5, n) for n in range(10)]
    assert [f.payload for f in frames] == frames_of(tone)


async def test_each_frames_ts_is_the_device_clock_at_its_first_sample(online: Online) -> None:
    before = await online.clock()
    await online.ok("mic", "start", {"stream": 5, "format": FORMAT})
    after = await online.clock()
    online.device.mic.feed(sources.tone(50), burst=True)

    frames = await online.udp.frames(5)

    assert before <= frames[0].ts <= after
    # The sample position is seq * 160 samples: 10 ms apart on the device clock.
    assert [f.ts - frames[0].ts for f in frames] == [n * 10_000 for n in range(5)]


async def test_a_mic_fed_nothing_streams_silence_in_real_time(online: Online) -> None:
    await online.ok("mic", "start", {"stream": 5, "format": FORMAT})

    frames = await online.udp.frames(3)

    assert [(f.seq, f.payload) for f in frames] == [(n, bytes(320)) for n in range(3)]


async def test_fed_audio_is_captured_in_real_time_then_silence(online: Online) -> None:
    online.device.mic.feed(sources.tone(20))
    await online.ok("mic", "start", {"stream": 5, "format": FORMAT})

    frames = await online.udp.frames(3)

    assert [f.payload for f in frames] == [*frames_of(sources.tone(20)), bytes(320)]


async def test_stop_replies_with_the_last_seq_once_the_final_frame_is_out(online: Online) -> None:
    await online.ok("mic", "start", {"stream": 5, "format": FORMAT})
    online.device.mic.feed(sources.tone(50), burst=True)

    result = await online.ok("mic", "stop")

    await asyncio.sleep(0.02)  # nothing more follows
    frames = online.udp.pending()
    assert result == {"last_seq": frames[-1].seq, "dropped": 0}
    assert [f.seq for f in frames] == list(range(len(frames)))
    assert online.device.mic.stream is None


async def test_stop_with_no_stream_open_replies_null(online: Online) -> None:
    assert await online.ok("mic", "stop") == {"last_seq": None, "dropped": 0}


async def test_stop_before_the_first_frame_is_captured_replies_null(online: Online) -> None:
    online.connection.send(Command(1, "mic", "start", {"stream": 5, "format": FORMAT}))
    online.connection.send(Command(2, "mic", "stop", {}))
    online.next_id = 3

    replies = [await online.connection.receive(), await online.connection.receive()]

    assert replies[1] == Reply(2, {"last_seq": None, "dropped": 0})


async def test_a_second_start_while_the_mic_streams_is_busy(online: Online) -> None:
    await online.ok("mic", "start", {"stream": 5, "format": FORMAT})

    reply = await online.command("mic", "start", {"stream": 6, "format": FORMAT})

    assert isinstance(reply, FailedReply)
    assert reply.code == "busy"


async def test_a_format_the_mic_doesnt_advertise_is_invalid(online: Online) -> None:
    reply = await online.command("mic", "start", {"stream": 5, "format": FORMAT | {"rate": 8000}})

    assert isinstance(reply, FailedReply)
    assert reply.code == "invalid_args"
    assert online.device.mic.stream is None


@pytest.mark.parametrize("stream", [0, 128])
async def test_a_stream_id_outside_the_hub_range_is_invalid(online: Online, stream: int) -> None:
    reply = await online.command("mic", "start", {"stream": stream, "format": FORMAT})

    assert isinstance(reply, FailedReply)
    assert reply.code == "invalid_args"
    assert online.device.mic.stream is None


async def test_the_echo_mic_is_busy_while_its_exclusive_peer_the_speaker_plays(
    online: Online,
) -> None:
    await online.ok("speaker", "start", {"stream": 1, "format": FORMAT})

    reply = await online.command("mic", "start", {"stream": 2, "format": FORMAT})

    assert isinstance(reply, FailedReply)
    assert reply.code == "busy"
    assert online.device.mic.stream is None


async def test_the_echo_speaker_is_busy_while_its_exclusive_peer_the_mic_streams(
    online: Online,
) -> None:
    await online.ok("mic", "start", {"stream": 1, "format": FORMAT})

    reply = await online.command("speaker", "start", {"stream": 2, "format": FORMAT})

    assert isinstance(reply, FailedReply)
    assert reply.code == "busy"
    assert online.device.speaker.stream is None


async def test_a_stop_then_start_pipelined_switches_the_exclusive_peer(online: Online) -> None:
    await online.ok("speaker", "start", {"stream": 1, "format": FORMAT})
    online.connection.send(Command(10, "speaker", "stop", {}))
    online.connection.send(Command(11, "mic", "start", {"stream": 2, "format": FORMAT}))

    stop, start = await online.connection.receive(), await online.connection.receive()

    assert isinstance(stop, Reply)
    assert start == Reply(11, {"format": FORMAT})
    assert online.device.mic.stream == 2


async def test_a_tx_stall_keeps_the_newest_200_ms_and_counts_the_rest_dropped(
    online: Online,
) -> None:
    await online.ok("mic", "start", {"stream": 5, "format": FORMAT})
    online.device.faults.mic_tx_stall = True
    tone = sources.tone(500)
    online.device.mic.feed(tone, burst=True)
    assert online.device.mic.dropped == 30

    online.device.faults.mic_tx_stall = False
    frames = await online.udp.frames(20)
    result = await online.ok("mic", "stop")

    assert [f.seq for f in frames] == list(range(30, 50))
    assert [f.payload for f in frames] == frames_of(tone)[30:]
    assert result["dropped"] == 30


async def test_a_stop_during_a_tx_stall_drops_what_is_queued(online: Online) -> None:
    await online.ok("mic", "start", {"stream": 5, "format": FORMAT})
    online.device.faults.mic_tx_stall = True
    online.device.mic.feed(sources.tone(100), burst=True)

    result = await online.ok("mic", "stop")

    assert result == {"last_seq": 9, "dropped": 10}
    online.device.faults.mic_tx_stall = False
    await asyncio.sleep(0.02)
    assert online.udp.pending() == []


async def test_the_device_shows_listening_while_its_mic_streams_even_through_an_led_claim(
    online: Online,
) -> None:
    await online.ok("led", "set", {"color": "#ff0000"})
    assert online.device.status == "claim"

    await online.ok("mic", "start", {"stream": 5, "format": FORMAT})
    assert online.device.status == "listening"
    assert online.device.lights()[0].color == "#00ff00"

    await online.ok("mic", "stop")
    assert online.device.status == "claim"


async def test_upstream_udp_faults_hit_the_mic_frames(online: Online) -> None:
    online.device.faults.udp_up.drop(5, {1, 2})
    await online.ok("mic", "start", {"stream": 5, "format": FORMAT})
    online.device.mic.feed(sources.tone(50), burst=True)

    frames = await online.udp.frames(3)

    assert [f.seq for f in frames] == [0, 3, 4]
    assert online.device.faults.udp_up.dropped == 2
