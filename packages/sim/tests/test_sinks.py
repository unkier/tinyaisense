"""Where a speaker's audio goes: the capture sink, the default, or none, or a WAV file."""

import wave
from array import array
from pathlib import Path

from tinyaisense_sim import sinks, sources
from tinyaisense_sim.speaker import volume_gain
from tinyaisense_wire import StreamFrame

from .raw_hub import CONN_ID, Online

FORMAT = {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": 10}


async def play(online: Online, pcm: bytes, lost: frozenset[int] = frozenset()) -> None:
    """Play `pcm` on the speaker to the end, but for the frames `lost` on the way."""
    await online.ok("speaker", "start", {"stream": 1, "format": FORMAT})
    frames = [pcm[at : at + 320] for at in range(0, len(pcm), 320)]
    for seq, frame in enumerate(frames):
        if seq not in lost:
            online.udp.send(StreamFrame(CONN_ID, 1, seq, 0, frame), online.device.udp_port)
    await online.ok("speaker", "drain", {"last_seq": len(frames) - 1})
    await online.device.wait_for(lambda: online.device.speaker.stream is None)


def scaled(pcm: bytes, gain: float) -> bytes:
    samples = array("h")
    samples.frombytes(pcm)
    return array("h", (round(s * gain) for s in samples)).tobytes()


async def test_the_capture_sink_is_the_default_and_keeps_every_frame(online: Online) -> None:
    tone = sources.tone(100)

    await play(online, tone)

    assert isinstance(online.device.speaker.sink, sinks.Capture)
    assert online.device.speaker.pcm() == tone


async def test_the_null_sink_keeps_nothing(online: Online) -> None:
    online.device.speaker.sink = sinks.Null()

    await play(online, sources.tone(100))

    assert online.device.speaker.captured == []


async def test_a_wav_sink_writes_what_is_heard_at_the_speakers_volume(
    online: Online, tmp_path: Path
) -> None:
    path = tmp_path / "kitchen.wav"
    online.device.speaker.sink = sinks.Wav(path)
    await online.ok("speaker", "set_volume", {"level": 50})
    tone = sources.tone(100)

    await play(online, tone, lost=frozenset({3}))
    await online.device.close()

    with wave.open(str(path), "rb") as f:
        assert (f.getframerate(), f.getnchannels(), f.getsampwidth()) == (16000, 1, 2)
        heard = f.readframes(f.getnframes())
    silenced = tone[: 3 * 320] + bytes(320) + tone[4 * 320 :]
    assert heard == scaled(silenced, volume_gain(50))
