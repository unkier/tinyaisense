"""The mic Kind on a simulated device: docs/protocol/kinds/mic.md."""

import asyncio
from collections import deque
from collections.abc import Callable

from tinyaisense_wire import AudioFormat, JsonObject

QUEUE_MS = 200
"""A stream's send queue: on overflow, the oldest frames are dropped."""

type SendFrame = Callable[[int, int, int, bytes], None]
"""`send(stream, seq, ts, payload)`: one frame, out over UDP."""


class SimMic:
    """One mic capability.

    It captures what it's fed, in real time: one frame per frame period while it streams,
    silence once the fed audio runs out. Audio fed with `burst=True` is captured at once
    instead. Each frame goes through the stream's 200 ms send queue, which the `mic_tx_stall`
    fault holds up.
    """

    def __init__(
        self,
        clock: Callable[[], int],
        send: SendFrame,
        stalled: Callable[[], bool],
        changed: Callable[[], None],
    ) -> None:
        """`clock()` is the device clock in µs; `stalled()` whether sending is held up;
        `changed()` is called whenever the mic starts or stops."""
        self._clock = clock
        self._send = send
        self._stalled = stalled
        self._changed = changed
        self._paced = bytearray()
        self._burst = bytearray()
        self._capture: _Capture | None = None

    @property
    def stream(self) -> int | None:
        """The stream id open, if any."""
        return None if self._capture is None else self._capture.stream

    @property
    def dropped(self) -> int:
        """Frames the open stream's queue dropped so far."""
        return 0 if self._capture is None else self._capture.dropped

    def feed(self, pcm: bytes, *, burst: bool = False) -> None:
        """Queue audio for the mic to hear, in its format, e.g. from `sources`. It's captured
        while the mic streams, after what was fed before: in real time, or with `burst` at
        once."""
        (self._burst if burst else self._paced).extend(pcm)
        if burst and self._capture is not None:
            self._capture.burst(self._burst)

    def start(self, stream: int, format: AudioFormat) -> None:
        """Open stream id `stream`. The caller checks that the mic is idle."""
        assert self._capture is None
        self._capture = _Capture(
            stream, format, self._clock(), self._paced, self._send, self._stalled
        )
        self._capture.burst(self._burst)
        self._changed()

    def stop(self) -> JsonObject:
        """Close the stream once its last frame has gone out: `{last_seq, dropped}`. If
        sending is held up, what's still queued is dropped. With nothing open, `last_seq` is
        null."""
        capture, self._capture = self._capture, None
        if capture is None:
            return {"last_seq": None, "dropped": 0}
        capture.close()
        self._changed()
        return {"last_seq": capture.seq - 1 if capture.seq else None, "dropped": capture.dropped}

    def cut(self) -> None:
        """The Connection is over: stop, and tell nobody."""
        if self._capture is not None:
            self.stop()

    def resume(self) -> None:
        """Sending is no longer held up: send what's queued."""
        if self._capture is not None:
            self._capture.flush()


class _Capture:
    """One stream, from `start` to `stop`."""

    def __init__(
        self,
        stream: int,
        format: AudioFormat,
        started: int,
        paced: bytearray,
        send: SendFrame,
        stalled: Callable[[], bool],
    ) -> None:
        self.stream = stream
        self.seq = 0
        """The next frame's seq."""
        self.dropped = 0
        self._format = format
        self._started = started
        """The device clock at the first sample."""
        self._paced = paced
        self._send = send
        self._stalled = stalled
        self._queue: deque[tuple[int, int, bytes]] = deque()
        self._queue_frames = QUEUE_MS // format.frame_ms
        self._loop = asyncio.get_running_loop()
        self._start = self._loop.time()
        self._timer: asyncio.TimerHandle | None = None
        self._arm()

    def burst(self, pcm: bytearray) -> None:
        """Capture `pcm` at once, and take it."""
        size = self._format.frame_bytes
        while pcm:
            self._captured(bytes(pcm[:size]))
            del pcm[:size]
        self._arm()

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._stalled():
            self.dropped += len(self._queue)
            self._queue.clear()
        self.flush()

    def flush(self) -> None:
        while self._queue and not self._stalled():
            self._send(self.stream, *self._queue.popleft())

    def _tick(self) -> None:
        # Every frame whose period has ended, catching up on a late wakeup.
        due = int((self._loop.time() - self._start) / self._period)
        size = self._format.frame_bytes
        while self.seq < due:
            self._captured(bytes(self._paced[:size]))
            del self._paced[:size]
        self._arm()

    def _captured(self, pcm: bytes) -> None:
        """A frame is in from the DMA: queue it and send."""
        frame = pcm.ljust(self._format.frame_bytes, b"\0")
        ts = self._started + self.seq * self._format.frame_ms * 1000
        self._queue.append((self.seq, ts, frame))
        if len(self._queue) > self._queue_frames:
            self._queue.popleft()
            self.dropped += 1
        self.seq += 1
        self.flush()

    def _arm(self) -> None:
        """Wake up when the next frame's period ends."""
        if self._timer is not None:
            self._timer.cancel()
        at = self._start + (self.seq + 1) * self._period
        self._timer = self._loop.call_at(at, self._tick)

    @property
    def _period(self) -> float:
        return self._format.frame_ms / 1000
