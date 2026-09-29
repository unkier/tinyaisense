"""Agent speaker streams, paced to their devices: docs/protocol/audio.md#speaker-stream."""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from tinyaisense_wire import KINDS, JsonObject, Schema, SchemaError, validate

from .alarm import Alarm, now_us
from .config import Timing
from .devices import DeviceConnection
from .keepalive import Tick
from .pacer import Close, Drain, Feed, Open, Overflow, Pacer, Remove, Send, Starved
from .resampler import Resampler
from .streams import FORMAT, Stream

if TYPE_CHECKING:
    from .session import Session  # it holds its streams

STATS = ("played_ms", "lost", "underruns", "underrun_ms")

_log = logging.getLogger("tinyaisense_hub.speaker")


class SpeakerStream(Stream):
    """One agent speaker stream. It takes the agent's audio while it's `open`."""

    sent_bytes = 0
    first_sent: int | None = None
    """When the first frame went out, in µs on the hub's monotonic clock."""
    on_starved: Callable[[], None] | None = None
    """Called when the stream has sent every whole frame it was fed, and isn't draining."""

    def __init__(
        self,
        handle: int | None,
        session: Session,
        conn: DeviceConnection,
        cap: str,
        stream_id: int,
        address: str,
        rate: int,
    ) -> None:
        super().__init__(handle, session, conn, cap, stream_id, address, rate)
        self._resampler = None if rate == FORMAT.rate else Resampler(rate, FORMAT.rate)
        """From the agent's rate to the device's; None if they're the same."""

    def to_device(self, pcm: bytes) -> bytes:
        """The agent's audio, at the device's rate."""
        return pcm if self._resampler is None else self._resampler.process(pcm)

    def tail(self) -> bytes:
        """The last of the agent's audio, which the resampler still holds: none follows."""
        return b"" if self._resampler is None else self._resampler.flush()

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
        """Take the agent's audio, at the stream's rate. Returns whether the stream overflowed
        its buffer: the pacer has forgotten it, and it must be stopped."""
        return self._run(Feed(stream, stream.to_device(pcm)))

    def drain(self, stream: SpeakerStream) -> None:
        """No more audio follows: once it's all sent, the device gets `drain`."""
        self._run(Close(stream, stream.tail()))

    def remove(self, stream: SpeakerStream) -> None:
        self._run(Remove(stream))

    def unsent(self, stream: SpeakerStream) -> int:
        """The bytes of the stream's audio not sent yet, at the device's rate."""
        return self._pacer.unsent(stream)

    def starved(self, stream: SpeakerStream) -> bool:
        """Whether the stream is open, and has sent every whole frame it was fed."""
        return self._pacer.starved(stream)

    def due(self, stream: SpeakerStream, position: int) -> int | None:
        """When the audio `position` bytes into the stream is due to play, in µs on the
        hub's monotonic clock; None before its first frame went out."""
        return self._pacer.due(stream, position)

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
                case Starved(stream):
                    if stream.on_starved is not None:
                        stream.on_starved()
        self._alarm.set(self._pacer.next_deadline(), now)
        return overflowed
