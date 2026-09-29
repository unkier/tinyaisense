"""One listen's VAD segmentation and end conditions. Sans-IO.

The VAD rates the listen's audio a window at a time. Speech starts at a window it rates at
least `START`, and carries on until `segment_silence_ms` of windows rated under `END`. Speech
of at least `MIN_SPEECH_MS` is a segment, from `PRE_ROLL_MS` before it starts; shorter speech
is noise. Segments are cut on frame boundaries, and positions are in samples from the start
of the listen's audio.

At each segment end the end-of-turn model gives its verdict. A complete turn ends the listen;
an incomplete one keeps it going, until `turn_silence_ms` of silence ends it anyway. Silence is
measured on the audio; the listen's own time limits, on the hub's monotonic clock in µs.
"""

from dataclasses import dataclass
from typing import Literal

from ..config import Timing
from ..keepalive import Tick
from ..streams import FORMAT

type EndReason = Literal[
    "stopped", "end_of_turn", "no_speech", "max_duration", "button", "click", "disconnected"
]
"""Why an Utterance ended: docs/protocol/agent.md#utterances."""

START = 0.5
"""A window rated at least this starts speech."""
END = 0.35
"""A window rated under this is silence, once speech has started."""
MIN_SPEECH_MS = 200
PRE_ROLL_MS = 300

_RATE = FORMAT.rate
_FRAME = FORMAT.frame_bytes // 2
"""Samples per frame."""


@dataclass(frozen=True, slots=True)
class Window:
    """The VAD's rating of the next window of audio: how likely it is speech, 0 to 1."""

    p: float


@dataclass(frozen=True, slots=True)
class TurnVerdict:
    """The end-of-turn model's verdict on the audio up to the end of segment `segment`."""

    segment: int
    complete: bool


@dataclass(frozen=True, slots=True)
class Stop:
    """The listen ends from outside, e.g. the agent stopped it or the device dropped."""

    reason: EndReason


@dataclass(frozen=True, slots=True)
class Segment:
    """Samples `start` to `end` of the audio are speech: transcribe them. With `turn`, ask the
    end-of-turn model too, and hand its verdict back."""

    index: int
    start: int
    end: int
    turn: bool


@dataclass(frozen=True, slots=True)
class End:
    """The listen is over. Any `Segment` still to come was handed out before this."""

    reason: EndReason


class Listening:
    """One listen, from its mic's start until it `End`s."""

    def __init__(self, now: int, timing: Timing, *, window: int) -> None:
        """`now` is when the mic started; `window` the samples per VAD window."""
        self._window = window
        self._segment_silence = _samples(timing.segment_silence_ms)
        self._turn_silence = _samples(timing.turn_silence_ms)
        self._no_speech_at: int | None = now + timing.no_speech_ms * 1000
        """Until speech is heard."""
        self._max_at = now + timing.listen_max_ms * 1000
        self._pos = 0
        """Samples rated so far."""
        self._speech: int | None = None
        """Where the speech going on started."""
        self._quiet: int | None = None
        """Where the silence going on within speech started."""
        self._last_speech: int | None = None
        """Where the latest speech ended, once a segment has: the turn's silence counts from
        there."""
        self._cut = 0
        """Where the last segment ended: the next one's pre-roll starts no earlier."""
        self._segments = 0
        self._ended = False

    def handle(self, input: Window | TurnVerdict | Stop | Tick, now: int) -> list[Segment | End]:
        if self._ended:
            return []
        actions: list[Segment | End] = []
        match input:
            case Window(p):
                self._rate(p, now, actions)
            case TurnVerdict(segment, complete):
                if complete and segment == self._segments - 1 and self._speech is None:
                    self._end("end_of_turn", actions)
            case Stop(reason):
                self._end(reason, actions)
            case Tick():
                if now >= self._max_at:
                    self._end("max_duration", actions)
                elif self._no_speech_due(now):
                    self._end("no_speech", actions)
        return actions

    def next_deadline(self) -> int | None:
        if self._ended:
            return None
        if self._no_speech_at is not None and self._speech is None:
            return min(self._no_speech_at, self._max_at)
        return self._max_at

    def _rate(self, p: float, now: int, actions: list[Segment | End]) -> None:
        at = self._pos
        self._pos += self._window
        if self._speech is None:
            if p >= START:
                self._speech = at
            elif (
                self._last_speech is not None
                and self._pos - self._last_speech >= self._turn_silence
            ):
                self._end("end_of_turn", actions)
            return
        if p >= START:
            self._quiet = None
        elif p < END:
            if self._quiet is None:
                self._quiet = at
            if self._pos - self._quiet >= self._segment_silence:
                self._end_speech(self._quiet, turn=True, actions=actions)
                if self._no_speech_due(now):
                    self._end("no_speech", actions)
                return
        if self._long_enough(self._so_far):
            self._no_speech_at = None  # speech is heard, even before its segment ends

    @property
    def _so_far(self) -> int:
        """Where the speech going on has got to: the silence within it doesn't count."""
        return self._pos if self._quiet is None else self._quiet

    def _no_speech_due(self, now: int) -> bool:
        return self._no_speech_at is not None and self._speech is None and now >= self._no_speech_at

    def _long_enough(self, end: int) -> bool:
        assert self._speech is not None
        return end - self._speech >= _samples(MIN_SPEECH_MS)

    def _end_speech(self, end: int, *, turn: bool, actions: list[Segment | End]) -> None:
        """The speech going on ended at `end`: a segment, if it's long enough."""
        assert self._speech is not None
        if self._long_enough(end):
            start = max(self._cut, _floor(self._speech - _samples(PRE_ROLL_MS)))
            self._cut = _ceil(self._pos)
            actions.append(Segment(self._segments, start, self._cut, turn=turn))
            self._segments += 1
            self._no_speech_at = None
        if self._segments:
            self._last_speech = end
        self._speech = self._quiet = None

    def _end(self, reason: EndReason, actions: list[Segment | End]) -> None:
        """End the listen. The speech going on, if long enough, is its last segment."""
        if self._speech is not None:
            self._end_speech(self._so_far, turn=False, actions=actions)
        self._ended = True
        actions.append(End(reason))


def _samples(ms: int) -> int:
    return ms * _RATE // 1000


def _floor(samples: int) -> int:
    return samples - samples % _FRAME


def _ceil(samples: int) -> int:
    return -(-samples // _FRAME) * _FRAME
