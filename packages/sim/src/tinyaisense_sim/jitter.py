"""The speaker's elastic jitter buffer: docs/protocol/audio.md#speaker-stream.

A twin of the firmware's tas_jbuf, minus the ramps: the Simulator doesn't model DSP. The UDP
side pushes frames by seq; the playout clock pulls one frame per frame period, whatever the
buffer holds.
"""

from dataclasses import dataclass, field
from typing import Literal

from tinyaisense_wire import AudioFormat

CAPACITY_MS = 200
"""The buffer spans this much of the stream from the playhead."""

type Outcome = Literal["prefill", "play", "lost", "underrun", "done"]
"""What a frame period played:
- `prefill`: not started yet, silence;
- `play`: a frame of the stream;
- `lost`: a missing frame, concealed as silence;
- `underrun`: paused on an empty buffer, silence, and the position holds;
- `done`: played through the drain's `last_seq`, silence.
"""


@dataclass(frozen=True, slots=True)
class Pulled:
    outcome: Outcome
    seq: int | None
    """The frame played or lost; None for the other outcomes."""
    pcm: bytes
    """The frame played, which may be a short final one; empty for silence."""


@dataclass(slots=True)
class Stats:
    played_ms: int = 0
    """The stream position reached: lost frames count, pauses don't."""
    lost: int = 0
    """Frames never played: concealed as silence, or skipped."""
    underruns: int = 0
    """Times playback paused on an empty buffer."""
    underrun_ms: int = 0
    """Total time paused."""
    short_frames: int = 0
    """Short frames dropped because they weren't the final one."""


@dataclass(slots=True)
class JitterBuffer:
    """One stream's jitter buffer. Pushing and pulling are the only ways in."""

    format: AudioFormat
    prefill_ms: int
    capacity_ms: int = CAPACITY_MS
    stats: Stats = field(default_factory=Stats)
    _frames: dict[int, bytes] = field(default_factory=dict[int, bytes])
    _head: int = 0
    """The next seq to play."""
    _last_seq: int | None = None
    """Set by the drain."""
    _started: bool = False
    _paused: bool = False
    _position: int = 0
    """Samples."""

    @property
    def _slots(self) -> int:
        return self.capacity_ms // self.format.frame_ms

    @property
    def _prefill_frames(self) -> int:
        """At least the first frame, and never more than the buffer holds."""
        frames = -(-self.prefill_ms // self.format.frame_ms)
        return min(max(frames, 1), self._slots)

    @property
    def draining(self) -> bool:
        return self._last_seq is not None

    def push(self, seq: int, pcm: bytes) -> bool:
        """Take frame `seq`. False if it's dropped: empty or longer than a frame, late, a
        duplicate, or past `last_seq`."""
        if not pcm or len(pcm) > self.format.frame_bytes:
            return False
        if seq < self._head or seq in self._frames:
            return False
        if self._last_seq is not None and seq > self._last_seq:
            return False
        self._frames[seq] = pcm
        if seq - self._head >= self._slots:
            # Drop what the buffer no longer spans, then anything missing up to the oldest
            # frame left.
            span = seq - self._slots + 1
            self._frames = {s: p for s, p in self._frames.items() if s >= span}
            self._skip_to_oldest()
        return True

    def drain(self, last_seq: int) -> None:
        """No frames follow `last_seq`. Starts playback if it's still prefilling."""
        self._last_seq = last_seq
        self._frames = {s: p for s, p in self._frames.items() if s <= last_seq}

    def pull(self) -> Pulled:
        """The next frame period of playout."""
        if not self._started:
            if not self.draining and self._held_from(self._head) < self._prefill_frames:
                return Pulled("prefill", None, b"")
            self._started = True
        # Resuming from a pause: the pause already stood in for any frames still missing.
        if self._paused:
            self._skip_to_oldest()
        if self._last_seq is not None and self._head > self._last_seq:
            return Pulled("done", None, b"")

        seq = self._head
        can_skip = self.draining or self._held_from(seq + 1) > 0  # past a missing frame
        if seq in self._frames and not self._whole(seq) and can_skip:
            # A short frame is legal only as the final one, and this one can't be.
            del self._frames[seq]
            self.stats.short_frames += 1
        if self._whole(seq) or can_skip:
            self._paused = False
            self._head += 1
            pcm = self._frames.pop(seq, None)
            if pcm is not None:
                self._advance(len(pcm) // (2 * self.format.channels))
                return Pulled("play", seq, pcm)
            self.stats.lost += 1
            self._advance(self.format.frame_samples)
            return Pulled("lost", seq, b"")
        # An empty buffer, or a short frame that may yet turn out to be the final one.
        if not self._paused:
            self.stats.underruns += 1
        self._paused = True
        self.stats.underrun_ms += self.format.frame_ms
        return Pulled("underrun", None, b"")

    def _whole(self, seq: int) -> bool:
        """Whether frame `seq` is held and playable: full length, or the short final one."""
        pcm = self._frames.get(seq)
        if pcm is None:
            return False
        return len(pcm) == self.format.frame_bytes or seq == self._last_seq

    def _held_from(self, first: int) -> int:
        return sum(1 for seq in self._frames if seq >= first)

    def _skip_to_oldest(self) -> None:
        """Move the playhead to the oldest frame held, if any. The frames it passes are
        lost."""
        oldest = min((seq for seq in self._frames if seq >= self._head), default=None)
        if oldest is None or oldest == self._head:
            return
        skipped = oldest - self._head
        self._head = oldest
        self.stats.lost += skipped
        self._advance(skipped * self.format.frame_samples)

    def _advance(self, samples: int) -> None:
        self._position += samples
        self.stats.played_ms = self._position * 1000 // self.format.rate
