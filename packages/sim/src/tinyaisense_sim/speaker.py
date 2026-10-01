"""The speaker Kind on a simulated device: docs/protocol/kinds/speaker.md."""

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import asdict

from tinyaisense_wire import AudioFormat, JsonObject

from .clock import SLACK_US, DeviceClock
from .jitter import JitterBuffer, Stats
from .sinks import Capture, CapturedFrame, Sink


def volume_gain(level: int) -> float:
    """The software gain of a `set_volume` level: 0 is mute, 1 is -40 dB and 100 is 0 dB,
    evenly spaced in dB between."""
    if level == 0:
        return 0.0
    return 10 ** (-40 * (100 - level) / 99 / 20)


class SimSpeaker:
    """One speaker capability. Its volume lasts until the device powers off.

    Playout runs in real time on the device clock: the jitter buffer gives up one frame per
    frame period from `start` on, and what it gives up is heard `output_latency_ms` later, once
    through the DMA.
    """

    def __init__(
        self,
        clock: DeviceClock,
        level: int,
        output_latency_ms: int,
        drained: Callable[[int, JsonObject], None],
        changed: Callable[[], None],
    ) -> None:
        """`drained(stream, stats)` is called once a drained stream has been heard to the end;
        `changed()` whenever the speaker starts, stops or plays a frame."""
        self.level = level
        """The volume, 0 to 100."""
        self.sink: Sink = Capture()
        """Where the audio goes: see `sinks`."""
        self.first_frame_at: float | None = None
        """When the latest stream's first frame came in off the network, on the event loop's
        clock: None until it does."""
        self._clock = clock
        self._latency = output_latency_ms / 1000
        self._drained = drained
        self._changed = changed
        self._playout: _Playout | None = None

    @property
    def captured(self) -> list[CapturedFrame]:
        """Every frame played since power-on, as it enters the DMA, into the capture sink: the
        latest may not have been heard yet. Empty with another sink."""
        return self.sink.frames if isinstance(self.sink, Capture) else []

    @property
    def gain(self) -> float:
        """What the speaker scales audio by, at its volume."""
        return volume_gain(self.level)

    @property
    def stream(self) -> int | None:
        """The stream id playing, if any."""
        return None if self._playout is None else self._playout.stream

    def pcm(self, stream: int | None = None) -> bytes:
        """The audio heard, lost frames as silence, before the volume: of stream id `stream`,
        or of every stream."""
        return b"".join(f.pcm for f in self.captured if stream is None or f.stream == stream)

    def start(self, stream: int, format: AudioFormat, prefill_ms: int) -> None:
        """Open stream id `stream`. The caller checks that nothing is playing."""
        assert self._playout is None
        self.first_frame_at = None
        self._playout = _Playout(
            stream, format, prefill_ms, self._clock, self._latency, self._pulled
        )
        self._changed()

    def push(self, seq: int, pcm: bytes) -> None:
        """A frame of the stream playing, off the network. Raises `WireError` if it breaks the
        frame rules. A short frame waits in the jitter buffer until the final seq is known."""
        assert self._playout is not None
        self._playout.buffer.format.check_frame(pcm, final=True)
        if self.first_frame_at is None:
            self.first_frame_at = asyncio.get_running_loop().time()
        self._playout.buffer.push(seq, pcm)

    def drain(self, last_seq: int | None) -> None:
        """No frames follow `last_seq`: None if the stream carried none. `drained` follows
        once the last one has been heard."""
        playout = self._playout
        if playout is None:
            return
        if last_seq is None:
            self._finish(playout)
            self._drained(playout.stream, _stats(playout.heard_by(float("inf"))))
        else:
            playout.buffer.drain(last_seq)

    def stop(self) -> JsonObject:
        """Cut playback. Returns the stats of what was heard; all 0 with nothing playing."""
        playout = self._playout
        if playout is None:
            return _stats(Stats())
        self._finish(playout)
        now = asyncio.get_running_loop().time()
        self.sink.cut(now)  # what was still in the DMA is never heard
        return _stats(playout.heard_by(now))

    def cut(self) -> None:
        """The Connection is over: stop, and tell nobody."""
        if self._playout is not None:
            self.stop()

    def close(self) -> None:
        """The device is off."""
        self.cut()
        self.sink.close()

    def _finish(self, playout: _Playout) -> None:
        playout.cancel()
        self._playout = None
        self._changed()

    def _pulled(self, playout: _Playout, frame: CapturedFrame | None) -> None:
        if frame is not None:
            self.sink.play(frame, self.gain)
            self._changed()
        elif playout is self._playout:  # done: heard to the end once through the DMA
            self._finish(playout)
            self._drained(playout.stream, _stats(playout.heard))


class _Playout:
    """One stream's jitter buffer, pulled on the playout clock."""

    def __init__(
        self,
        stream: int,
        format: AudioFormat,
        prefill_ms: int,
        clock: DeviceClock,
        latency: float,
        pulled: Callable[[_Playout, CapturedFrame | None], None],
    ) -> None:
        self.stream = stream
        self.buffer = JitterBuffer(format, prefill_ms)
        self._format = format
        self._clock = clock
        self._latency = latency
        self._pulled = pulled
        self._loop = asyncio.get_running_loop()
        self._started = clock.now()
        self._period = format.frame_ms * 1000
        """In device µs."""
        self._pulls = 0
        self._pending: deque[tuple[float, Stats]] = deque()
        """The stats after each pull, from when it's heard, until then."""
        self.heard = Stats()
        """The stats of what has been heard so far."""
        self._timer: asyncio.TimerHandle | None = None
        self._tick()

    def heard_by(self, now: float) -> Stats:
        while self._pending and self._pending[0][0] <= now:
            self.heard = self._pending.popleft()[1]
        return self.heard

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _tick(self) -> None:
        now = self._loop.time()
        # Catch up on the periods a late wakeup missed. A frame can't play before now, since
        # it may have only just arrived.
        due = (self._clock.now() + SLACK_US - self._started) // self._period + 1
        while self._pulls < due:
            at = now + self._clock.delay(self._started + self._pulls * self._period)
            at += self._latency
            self._pulls += 1
            pulled = self.buffer.pull()
            if pulled.outcome == "done":
                self._timer = self._loop.call_at(at, self._done)
                return
            self._pending.append((at, _copy(self.buffer.stats)))
            if pulled.seq is not None:
                pcm = pulled.pcm or bytes(self._format.frame_bytes)
                frame = CapturedFrame(self.stream, pulled.seq, pcm, at, pulled.outcome == "lost")
                self._pulled(self, frame)
        next_pull = self._clock.delay(self._started + self._pulls * self._period)
        self._timer = self._loop.call_later(next_pull, self._tick)

    def _done(self) -> None:
        self._timer = None
        self.heard_by(float("inf"))
        self._pulled(self, None)


def _copy(stats: Stats) -> Stats:
    return Stats(**asdict(stats))


def _stats(stats: Stats) -> JsonObject:
    return {
        "played_ms": stats.played_ms,
        "lost": stats.lost,
        "underruns": stats.underruns,
        "underrun_ms": stats.underrun_ms,
    }
