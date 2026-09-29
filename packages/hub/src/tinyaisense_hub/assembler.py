"""The mic assembler: one mic stream's frames, off the network, as a timeline. Sans-IO.

docs/protocol/audio.md#mic-stream. There's no reorder wait: a gap is zero-filled as soon as a
later frame arrives, and late frames and duplicates are dropped. After the `stop` reply's
`last_seq`, the missing tail frames get `tail` µs to arrive, then are zero-filled. Every time
is in µs on the hub's monotonic clock.
"""

from collections import Counter
from dataclasses import dataclass

from tinyaisense_wire import AudioFormat, WireError

from .keepalive import Tick

MAX_GAP = 6000
"""A seq further ahead than this many frames, of both the timeline and the time since the
stream's first input, is bogus: zero-filling up to it would flood the agent."""


@dataclass(frozen=True, slots=True)
class Frame:
    """A stream frame arrived."""

    seq: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class Last:
    """The device's final seq, from its `stop` reply: None if no frame went out."""

    last_seq: int | None


@dataclass(frozen=True, slots=True)
class Deliver:
    """The timeline's next frame: `index` frames from the start. Silence for a lost frame."""

    index: int
    pcm: bytes


@dataclass(frozen=True, slots=True)
class Finished:
    """The timeline is complete: nothing more follows."""


class MicAssembler:
    """One mic stream's timeline, from its first frame until it's `finished`."""

    def __init__(self, format: AudioFormat, *, tail: int) -> None:
        self._format = format
        self._tail = tail
        self._next = 0
        """The seq of the next frame to deliver."""
        self._last: int | None = None
        self._held: Frame | None = None
        """A short frame, waiting to learn whether it's the final one."""
        self._deadline: int | None = None
        self._origin: int | None = None
        """When the first input came."""
        self.finished = False
        self.frames = 0
        """Frames delivered, zero-filled ones included."""
        self.filled = 0
        """Frames zero-filled."""
        self.late = 0
        """Late frames and duplicates dropped."""
        self.rejected: Counter[str] = Counter()
        """Frames dropped for breaking the frame rules, or out of the stream, by why."""

    def handle(self, input: Frame | Last | Tick, now: int) -> list[Deliver | Finished]:
        if self.finished:
            return []
        if self._origin is None:
            self._origin = now
        actions: list[Deliver | Finished] = []
        match input:
            case Frame():
                self._frame(input, now, actions)
            case Last(last_seq):
                if last_seq is not None and self._bogus(last_seq, now):
                    self.rejected["bad_seq"] += 1
                    last_seq = self._next - 1  # the stream as it stands
                self._last = -1 if last_seq is None else last_seq
                if (held := self._held) is not None:
                    self._held = None
                    self._frame(held, now, actions)
                if not self._complete(actions):
                    self._deadline = now + self._tail
            case Tick():
                if self._deadline is not None and now >= self._deadline:
                    assert self._last is not None
                    self._fill(self._last + 1, actions)
                    self._complete(actions)
        return actions

    def next_deadline(self) -> int | None:
        """When to `handle(Tick())` next: the end of the tail wait, if one is on."""
        return None if self.finished else self._deadline

    def _frame(self, frame: Frame, now: int, actions: list[Deliver | Finished]) -> None:
        seq = frame.seq
        if seq < self._next:
            self.late += 1
            return
        if self._last is not None and seq > self._last:
            self.rejected["past_last"] += 1
            return
        if self._bogus(seq, now):
            self.rejected["bad_seq"] += 1
            return
        try:
            self._format.check_frame(frame.payload, final=True)
        except WireError as e:
            self.rejected[e.code] += 1
            return
        short = len(frame.payload) < self._format.frame_bytes
        if (held := self._held) is not None and seq >= held.seq:
            # A frame at or after the held short one: that one wasn't the final frame.
            self._held = None
            self.rejected["short_frame"] += 1
        if short and seq != self._last:
            if self._last is None and self._held is None:
                self._held = frame  # until the final seq is known
            else:
                self.rejected["short_frame"] += 1
            return
        self._fill(seq, actions)
        # Padded with silence if it's the short final frame: the timeline is whole frames.
        actions.append(Deliver(seq, frame.payload.ljust(self._format.frame_bytes, b"\0")))
        self._next = seq + 1
        self.frames += 1
        self._complete(actions)

    def _bogus(self, seq: int, now: int) -> bool:
        assert self._origin is not None
        elapsed = (now - self._origin) // (self._format.frame_ms * 1000)
        return seq - max(self._next, elapsed) > MAX_GAP

    def _fill(self, until: int, actions: list[Deliver | Finished]) -> None:
        """Zero-fill the timeline up to seq `until`, exclusive."""
        silence = bytes(self._format.frame_bytes)
        for index in range(self._next, until):
            actions.append(Deliver(index, silence))
        if until > self._next:
            self.filled += until - self._next
            self.frames += until - self._next
            self._next = until

    def _complete(self, actions: list[Deliver | Finished]) -> bool:
        """Finish, if every frame through the last seq is out. Returns whether finished."""
        if self.finished:
            return True
        if self._last is None or self._next <= self._last:
            return False
        self.finished = True
        self._deadline = None
        self._held = None
        actions.append(Finished())
        return True
