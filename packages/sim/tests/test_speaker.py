"""The speaker's volume curve (docs/protocol/kinds/speaker.md#set_volume-hub-only), and when
its frames come in."""

import asyncio
import math

import pytest
from tinyaisense_sim.speaker import volume_gain
from tinyaisense_wire import StreamFrame

from .raw_hub import CONN_ID, Online

FORMAT = {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": 10}


def send_prefill(online: Online, stream: int) -> None:
    """Send the device the 40 ms its speaker waits for before it plays."""
    for seq in range(4):
        online.udp.send(StreamFrame(CONN_ID, stream, seq, 0, bytes(320)), online.device.udp_port)


def db(gain: float) -> float:
    return 20 * math.log10(gain)


def test_level_0_mutes() -> None:
    assert volume_gain(0) == 0.0


def test_level_1_is_about_minus_40_db_and_level_100_is_0_db() -> None:
    assert db(volume_gain(1)) == pytest.approx(-40)
    assert volume_gain(100) == 1.0


def test_each_level_step_is_the_same_number_of_db() -> None:
    steps = [db(volume_gain(level + 1)) - db(volume_gain(level)) for level in range(1, 100)]

    assert steps == pytest.approx([40 / 99] * 99)


async def test_the_speaker_notes_when_its_streams_first_frame_came_in(online: Online) -> None:
    speaker = online.device.speaker
    await online.ok("speaker", "start", {"stream": 1, "format": FORMAT})
    assert speaker.first_frame_at is None
    await asyncio.sleep(0.05)

    sent = asyncio.get_running_loop().time()
    send_prefill(online, stream=1)
    await online.device.wait_for(lambda: bool(speaker.captured))

    assert speaker.first_frame_at is not None
    assert sent <= speaker.first_frame_at <= speaker.captured[0].at


async def test_a_new_stream_forgets_when_the_last_ones_first_frame_came_in(online: Online) -> None:
    speaker = online.device.speaker
    await online.ok("speaker", "start", {"stream": 1, "format": FORMAT})
    send_prefill(online, stream=1)
    await online.device.wait_for(lambda: bool(speaker.captured))
    await online.ok("speaker", "stop")

    await online.ok("speaker", "start", {"stream": 2, "format": FORMAT})

    assert speaker.first_frame_at is None
