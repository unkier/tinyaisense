"""The hub-wide speaker pacer. Sans-IO.

Agents send speaker audio as fast as they get it. The pacer holds each stream's audio, cuts
it into frames, and sends them in real time: the first 60 ms as a burst, then keeping a 60 ms
lead on the stream's schedule. Every time is in µs on the hub's monotonic clock.
"""

from collections.abc import Hashable
from dataclasses import dataclass, field

from .keepalive import Tick


@dataclass(frozen=True, slots=True)
class Open[K]:
    """A stream started on its device: frames of `frame_bytes` each."""

    key: K
    frame_bytes: int


@dataclass(frozen=True, slots=True)
class Feed[K]:
    """Audio arrived for the stream."""

    key: K
    pcm: bytes


@dataclass(frozen=True, slots=True)
class Close[K]:
    """No more audio follows: once the stream is sent, it drains."""

    key: K


@dataclass(frozen=True, slots=True)
class Remove[K]:
    """Forget the stream: it ended."""

    key: K


@dataclass(frozen=True, slots=True)
class Send[K]:
    """Send frame `seq` of the stream now."""

    key: K
    seq: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class Drain[K]:
    """The stream is closed and fully sent: send the device `drain{last_seq}`."""

    key: K
    last_seq: int | None


@dataclass(frozen=True, slots=True)
class Overflow[K]:
    """The stream's unsent audio went over the cap. The pacer has forgotten it."""

    key: K


@dataclass(slots=True)
class _Stream:
    frame_bytes: int
    source: bytearray = field(default_factory=bytearray)
    """Audio not sent yet."""
    sent: int = 0
    """Frames sent: the next frame's seq."""
    anchor: int | None = None
    """When frame 0 is due to play, by the schedule. Set by the first frame sent."""
    closed: bool = False
    drained: bool = False
    """Whether `Drain` went out."""

    def next_frame(self) -> int:
        """The size of the next frame to send, once it's due; 0 if there's none yet."""
        if len(self.source) >= self.frame_bytes:
            return self.frame_bytes
        return len(self.source) if self.closed else 0  # the short final frame


class Pacer[K: Hashable]:
    """Every speaker stream's source and schedule. Audio for a stream it doesn't hold, e.g.
    after an `Overflow`, is dropped."""

    def __init__(
        self, *, cap: int, frame: int = 10_000, lead: int = 60_000, tick: int = 10_000
    ) -> None:
        """`frame` is the duration of one frame, and `lead` how far ahead of its schedule
        each stream is sent. While any stream has a frame to send, the pacer ticks every
        `tick`. A stream whose unsent audio exceeds `cap` overflows."""
        self._cap = cap
        self._frame = frame
        self._lead = lead
        self._tick = tick
        self._streams: dict[K, _Stream] = {}
        self._next_tick: int | None = None

    def handle(
        self, input: Open[K] | Feed[K] | Close[K] | Remove[K] | Tick, now: int
    ) -> list[Send[K] | Drain[K] | Overflow[K]]:
        actions: list[Send[K] | Drain[K] | Overflow[K]] = []
        match input:
            case Open(key, frame_bytes):
                self._streams[key] = _Stream(frame_bytes)
            case Feed(key, pcm):
                stream = self._streams.get(key)
                if stream is not None and not stream.closed:
                    stream.source += pcm
                    actions += self._send_due(key, stream, now)
                    if len(stream.source) > self._cap_bytes(stream):
                        del self._streams[key]
                        actions.append(Overflow(key))
            case Remove(key):
                self._streams.pop(key, None)
            case Close(key):
                if (stream := self._streams.get(key)) is not None:
                    stream.closed = True
                    actions += self._send_due(key, stream, now)
            case Tick():
                self._next_tick = None
                for key, stream in self._streams.items():
                    actions += self._send_due(key, stream, now)
        if self._next_tick is None and any(s.next_frame() for s in self._streams.values()):
            self._next_tick = now + self._tick
        return actions

    def next_deadline(self) -> int | None:
        """When to `handle(Tick())` next; None while there's nothing to send."""
        return self._next_tick

    def _cap_bytes(self, stream: _Stream) -> int:
        return self._cap * stream.frame_bytes // self._frame

    def _send_due(self, key: K, stream: _Stream, now: int) -> list[Send[K] | Drain[K]]:
        sent: list[Send[K] | Drain[K]] = []
        while size := stream.next_frame():
            n = stream.sent
            # Behind schedule, the device has played out what it had: start the schedule
            # afresh, so it gets a new burst instead of everything that fell due meanwhile.
            anchor = now - n * self._frame
            if stream.anchor is None or stream.anchor < anchor:
                stream.anchor = anchor
            if stream.anchor + n * self._frame >= now + self._lead:
                break
            payload = bytes(stream.source[:size])
            del stream.source[:size]
            stream.sent += 1
            sent.append(Send(key, n, payload))
        if stream.closed and not stream.source and not stream.drained:
            stream.drained = True
            sent.append(Drain(key, stream.sent - 1 if stream.sent else None))
        return sent
