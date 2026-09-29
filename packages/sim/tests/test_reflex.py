"""The Reflex on a simulated device, driven by a hand-rolled hub: docs/protocol/kinds/button.md.

The raw hub arms it as the real one does: cut the speaker, open the mic.
"""

from tinyaisense_sim import sources
from tinyaisense_wire import Event, FailedReply, Welcome

from .raw_hub import CONN_ID, Online, RawHub

FORMAT = {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": 10}
ARMED = {"armed": True, "stop": ["speaker"], "start": {"cap": "mic", "format": FORMAT}}


async def playing(online: Online, stream: int = 1) -> None:
    """Start the speaker on a hub stream."""
    await online.ok("speaker", "start", {"stream": stream, "format": FORMAT})


async def test_a_press_cuts_the_speaker_then_opens_the_mic_on_a_device_range_id(
    online: Online,
) -> None:
    assert await online.ok("button", "set_reflex", ARMED) == {}
    await playing(online)

    online.device.button.press()
    down, stopped, started = [await online.connection.event() for _ in range(3)]
    frame = await online.udp.frame()

    assert (down.name, down.cap) == ("down", "button")
    assert stopped == Event(1, "speaker", "stopped", stopped.ts, stopped.data)
    assert stopped.data | {"played_ms": 0} == {
        "stream": 1,
        "reason": "reflex",
        "played_ms": 0,
        "lost": 0,
        "underruns": 0,
        "underrun_ms": 0,
    }
    assert started == Event(2, "mic", "started", started.ts, {"stream": 128, "format": FORMAT})
    assert (frame.stream, frame.seq) == (128, 0)
    assert online.device.speaker.stream is None
    assert online.device.mic.stream == 128


async def test_a_release_closes_the_mic_the_reflex_opened(online: Online) -> None:
    await online.ok("button", "set_reflex", ARMED)
    online.device.button.press()
    [await online.connection.event() for _ in range(2)]
    frames = await online.udp.frames(3)

    online.device.button.release()
    up, stopped = [await online.connection.event() for _ in range(2)]

    assert (up.name, up.cap) == ("up", "button")
    assert stopped.cap == "mic"
    assert stopped.name == "stopped"
    assert stopped.data["stream"] == 128
    assert stopped.data["reason"] == "reflex"
    assert stopped.data["last_seq"] >= frames[-1].seq
    assert stopped.data["dropped"] == 0
    assert online.device.mic.stream is None


async def test_a_press_with_the_mic_on_a_hub_stream_sends_only_the_edges(online: Online) -> None:
    await online.ok("button", "set_reflex", ARMED)
    await online.ok("mic", "start", {"stream": 5, "format": FORMAT})

    await online.device.button.click()
    down, up = [await online.connection.event() for _ in range(2)]

    assert [down.name, up.name] == ["down", "up"]
    assert online.device.mic.stream == 5


async def test_a_release_after_the_hub_stopped_the_reflex_mic_sends_only_up(
    online: Online,
) -> None:
    await online.ok("button", "set_reflex", ARMED)
    online.device.button.press()
    [await online.connection.event() for _ in range(2)]
    await online.ok("mic", "stop")

    online.device.button.release()
    online.device.button.press()
    up, down, started = [await online.connection.event() for _ in range(3)]

    assert [up.name, down.name] == ["up", "down"]
    assert started.data["stream"] == 129


async def test_a_disarmed_button_sends_only_the_edges(online: Online) -> None:
    await online.ok("button", "set_reflex", ARMED)
    await online.ok("button", "set_reflex", {"armed": False})
    await playing(online)

    await online.device.button.click()
    down, up = [await online.connection.event() for _ in range(2)]

    assert [down.name, up.name] == ["down", "up"]
    assert online.device.speaker.stream == 1
    assert online.device.mic.stream is None


async def test_set_reflex_refuses_targets_the_device_lacks(online: Online) -> None:
    for args in (
        {"armed": True, "stop": ["mic"]},
        {"armed": True, "start": {"cap": "speaker", "format": FORMAT}},
        {"armed": True, "start": {"cap": "mic", "format": FORMAT | {"rate": 44100}}},
    ):
        reply = await online.command("button", "set_reflex", args)
        assert isinstance(reply, FailedReply)
        assert reply.code == "invalid_args"


async def test_a_drop_disarms_the_reflex_and_forgets_the_held_press(
    online: Online, raw_hub: RawHub
) -> None:
    await online.ok("button", "set_reflex", ARMED)
    online.device.button.press()
    [await online.connection.event() for _ in range(2)]

    online.connection.close()
    connection = await raw_hub.accept()
    connection.send(Welcome("kitchen", CONN_ID, online.udp.port))
    await online.device.wait_for(lambda: online.device.state == "online")
    online.device.button.release()
    online.device.button.press()

    # No `up` for the press held across the drop, and no mic: the drop disarmed the Reflex.
    down = await connection.event()
    assert down.name == "down"
    assert online.device.mic.stream is None


async def test_talk_presses_streams_the_audio_and_releases(online: Online) -> None:
    await online.ok("button", "set_reflex", ARMED)
    speech = sources.tone(50)

    await online.device.talk(speech)
    events = [await online.connection.event() for _ in range(4)]
    frames = online.udp.pending()

    assert [(e.cap, e.name) for e in events] == [
        ("button", "down"),
        ("mic", "started"),
        ("button", "up"),
        ("mic", "stopped"),
    ]
    assert b"".join(f.payload for f in frames)[: len(speech)] == speech
    assert events[3].data["last_seq"] == frames[-1].seq
    assert online.device.button.pressed is False


async def test_talk_to_a_disarmed_button_feeds_nothing(online: Online) -> None:
    await online.device.talk(sources.tone(50))
    await online.ok("mic", "start", {"stream": 5, "format": FORMAT})
    frame = await online.udp.frame()

    assert frame.payload == bytes(len(frame.payload))
