"""Agent speaker streams, paced to their devices: docs/protocol/audio.md#speaker-stream."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from tinyaisense_wire import KINDS, AudioFormat, JsonObject, Schema, SchemaError, validate

from .alarm import Alarm, now_us
from .config import Timing
from .devices import DeviceConnection
from .keepalive import Tick
from .pacer import Close, Drain, Feed, Open, Overflow, Pacer, Remove, Send

if TYPE_CHECKING:
    from .session import Session  # it holds its streams

type EndReason = Literal["drained", "stopped", "preempted", "reflex", "disconnected", "overflow"]

FORMAT = AudioFormat("pcm_s16le", 16000, 1, 10)
"""What the hub streams to every speaker: 10 ms frames of 16 kHz mono PCM."""
STATS = ("played_ms", "lost", "underruns", "underrun_ms")

_log = logging.getLogger("tinyaisense_hub.speaker")


@dataclass(frozen=True, slots=True)
class StreamEnd:
    reason: EndReason
    stats: JsonObject

    def to_json(self) -> JsonObject:
        return {"reason": self.reason, "stats": self.stats}


class SpeakerStream:
    """One agent speaker stream, from the device's `start` reply to its end."""

    def __init__(
        self,
        handle: int,
        session: Session,
        conn: DeviceConnection,
        cap: str,
        stream_id: int,
        address: str,
    ) -> None:
        self.handle = handle
        self.session = session
        self.conn = conn
        self.cap = cap
        self.stream_id = stream_id
        """The device stream id, 1-127."""
        self.address = address
        self.ended: asyncio.Future[StreamEnd] = asyncio.get_running_loop().create_future()
        self.stopping = False
        """A stop is on its way: the agent's audio is dropped."""
        self.sent_bytes = 0
        self.first_sent: int | None = None
        """When the first frame went out, in µs on the hub's monotonic clock."""

    @property
    def open(self) -> bool:
        """Whether the stream takes the agent's audio."""
        return not self.stopping and not self.ended.done()

    def estimate(self, now: int) -> JsonObject:
        """The stats, when the device can't report them: `played_ms` is the audio sent,
        capped by the time since the first frame went out."""
        played = 0
        if self.first_sent is not None:
            sent_ms = self.sent_bytes * FORMAT.frame_ms // FORMAT.frame_bytes
            played = min(sent_ms, (now - self.first_sent) // 1000)
        return {"played_ms": played, "lost": 0, "underruns": 0, "underrun_ms": 0}


def stats(schema: Schema, data: JsonObject) -> JsonObject | None:
    """The stats in a `stop` result or a `drained` event; None if they break `schema`."""
    try:
        validate(schema, data, "stats")
    except SchemaError as e:
        _log.warning("bad speaker stats: %s", e)
        return None
    return {key: data[key] for key in STATS}


STOP_RESULT = KINDS["speaker"].commands["stop"].result
DRAINED = KINDS["speaker"].events["drained"].data
STOPPED = KINDS["speaker"].events["stopped"].data


class Speakers:
    """The audio of every speaker stream on the hub, sent by one hub-wide pacer."""

    def __init__(self, timing: Timing, drain: Callable[[SpeakerStream, int | None], None]) -> None:
        """`drain(stream, last_seq)` is called once a drained stream's last frame is out."""
        self._pacer: Pacer[SpeakerStream] = Pacer(cap=timing.speaker_buffer_ms * 1000)
        self._alarm = Alarm(self._tick)
        self._drain = drain

    def open(self, stream: SpeakerStream) -> None:
        self._run(Open(stream, FORMAT.frame_bytes))

    def feed(self, stream: SpeakerStream, pcm: bytes) -> bool:
        """Take the agent's audio. Returns whether the stream overflowed its buffer: the
        pacer has forgotten it, and it must be stopped."""
        return self._run(Feed(stream, pcm))

    def drain(self, stream: SpeakerStream) -> None:
        """No more audio follows: once it's all sent, the device gets `drain`."""
        self._run(Close(stream))

    def remove(self, stream: SpeakerStream) -> None:
        self._run(Remove(stream))

    def close(self) -> None:
        self._alarm.cancel()

    def _tick(self, now: int) -> None:
        self._run(Tick(), now)

    def _run(
        self,
        input: Open[SpeakerStream]
        | Feed[SpeakerStream]
        | Close[SpeakerStream]
        | Remove[SpeakerStream]
        | Tick,
        now: int | None = None,
    ) -> bool:
        now = now_us() if now is None else now
        overflowed = False
        for action in self._pacer.handle(input, now):
            match action:
                case Send(stream, seq, payload):
                    if stream.first_sent is None:
                        stream.first_sent = now
                    stream.sent_bytes += len(payload)
                    stream.conn.send_frame(stream.stream_id, seq, payload)
                case Drain(stream, last_seq):
                    self._drain(stream, last_seq)
                case Overflow():
                    overflowed = True
        self._alarm.set(self._pacer.next_deadline(), now)
        return overflowed
