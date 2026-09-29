"""The listen core: VAD segmentation and end conditions, driven by VAD windows, end-of-turn
verdicts and a fake `now` in µs."""

from collections.abc import Sequence

from tinyaisense_hub import Timing
from tinyaisense_hub.keepalive import Tick
from tinyaisense_hub.speech.listening import (
    End,
    Hold,
    Listening,
    Segment,
    Stop,
    TurnVerdict,
    Window,
)

S = 1_000_000
"""One second in µs."""
W = 512
"""Samples per VAD window: 32 ms at 16 kHz."""

type Action = Segment | End


def listening(now: int = 0, **timing: int) -> Listening:
    return Listening(now, Timing(**timing), window=W)


def windows(core: Listening, probabilities: Sequence[float], now: int = 0) -> list[Action]:
    actions: list[Action] = []
    for p in probabilities:
        actions += core.handle(Window(p), now)
    return actions


def segment(core: Listening, probabilities: Sequence[float]) -> Segment:
    """The one segment the windows end."""
    [action] = windows(core, probabilities)
    assert isinstance(action, Segment)
    return action


def test_speech_then_200_ms_of_silence_is_a_segment_with_300_ms_of_pre_roll() -> None:
    core = listening()

    # Speech from window 20 (sample 10240) to window 30 (15360); the 7th silent window takes
    # the silence to 224 ms.
    assert windows(core, [0] * 20 + [1] * 10 + [0] * 6) == []
    actions = windows(core, [0])

    # 300 ms before 10240 is 5440, a frame boundary; the audio so far ends at 18944, which
    # rounds up to the next frame, 19040.
    assert actions == [Segment(0, start=5440, end=19040, turn=True)]


def test_speech_shorter_than_200_ms_is_noise() -> None:
    core = listening()

    # 6 windows is 192 ms.
    assert windows(core, [0] * 20 + [1] * 6 + [0] * 20) == []


def test_windows_between_the_thresholds_carry_on_the_speech_or_the_silence() -> None:
    core = listening()

    # 0.4 doesn't start speech; within speech, it doesn't count as silence.
    assert windows(core, [0.4] * 20 + [1] * 3 + [0.4] * 10 + [0] * 6) == []
    # Speech from 10240, with its pre-roll from 5440, to 16896; then 224 ms of silence.
    assert windows(core, [0]) == [Segment(0, start=5440, end=20480, turn=True)]


def test_a_segments_pre_roll_starts_no_earlier_than_the_last_segment_ended() -> None:
    core = listening()
    first = segment(core, [1] * 10 + [0] * 7)

    # The next speech starts at 9728, 64 ms after the first segment ended at 8704, rounded up
    # to 8800. Its segment ends at 18432, rounded up to 18560.
    second = segment(core, [0] * 2 + [1] * 10 + [0] * 7)

    assert first == Segment(0, start=0, end=8800, turn=True)
    assert second == Segment(1, start=8800, end=18560, turn=True)


SPEECH = [0] * 20 + [1] * 10 + [0] * 7
"""A segment, ending as the last window is rated: 640 ms of silence, 320 ms of speech and
224 ms of silence."""


def test_a_complete_turn_ends_the_listen() -> None:
    core = listening()
    said = segment(core, SPEECH)

    assert core.handle(TurnVerdict(said.index, complete=True), 0) == [End("end_of_turn")]
    assert core.next_deadline() is None
    assert windows(core, [1] * 20 + [0] * 10) == []


def test_an_incomplete_turn_keeps_listening_until_1_s_of_silence() -> None:
    core = listening()
    said = segment(core, SPEECH)
    assert core.handle(TurnVerdict(said.index, complete=False), 0) == []

    # The speech ended at 15360; 1 s later is 31360, in the 25th window after the segment.
    assert windows(core, [0] * 24) == []
    assert windows(core, [0]) == [End("end_of_turn")]


def test_1_s_of_silence_ends_the_turn_even_before_the_verdict() -> None:
    core = listening()
    windows(core, SPEECH)

    assert windows(core, [0] * 24) == []
    assert windows(core, [0]) == [End("end_of_turn")]


def test_speech_after_a_segment_outdates_its_verdict_and_its_silence() -> None:
    core = listening()
    first = segment(core, SPEECH)
    windows(core, [1] * 10)

    assert core.handle(TurnVerdict(first.index, complete=True), 0) == []
    second = segment(core, [0] * 7)
    assert second.index == 1
    assert windows(core, [0] * 20) == []
    assert core.handle(TurnVerdict(second.index, complete=True), 0) == [End("end_of_turn")]


def test_noise_after_a_segment_restarts_the_silence() -> None:
    core = listening()
    windows(core, SPEECH + [0] * 10)
    windows(core, [1] * 3)

    # The noise ended at window 50: 1 s later is in window 81.
    assert windows(core, [0] * 27) == []
    assert windows(core, [0] * 5) == [End("end_of_turn")]


def test_8_s_with_no_speech_ends_the_listen() -> None:
    core = listening(now=S)
    windows(core, [0] * 100 + [1] * 3 + [0] * 7, now=2 * S)  # noise

    assert core.next_deadline() == 9 * S
    assert core.handle(Tick(), 9 * S - 1) == []
    assert core.handle(Tick(), 9 * S) == [End("no_speech")]
    assert core.next_deadline() is None


def test_speech_of_200_ms_counts_as_speech_before_its_segment_ends() -> None:
    core = listening()
    windows(core, [1] * 7)

    assert core.next_deadline() == 60 * S
    assert core.handle(Tick(), 8 * S) == []


def test_speech_going_on_at_8_s_holds_off_no_speech_until_it_turns_out_noise() -> None:
    core = listening()
    windows(core, [1] * 3)

    assert core.next_deadline() == 60 * S
    assert core.handle(Tick(), 8 * S) == []
    assert windows(core, [0] * 6, now=8 * S) == []
    assert windows(core, [0], now=8 * S) == [End("no_speech")]


def test_the_listen_ends_after_60_s_and_the_speech_going_on_is_a_last_segment() -> None:
    core = listening()
    windows(core, [0] * 10 + [1] * 10 + [0] * 2)

    assert core.handle(Tick(), 60 * S) == [
        # From 300 ms before 5120 to 11264, all the audio so far.
        Segment(0, start=320, end=11360, turn=False),
        End("max_duration"),
    ]
    assert core.next_deadline() is None


def test_a_stop_ends_the_listen_with_its_reason_and_noise_is_no_last_segment() -> None:
    core = listening()
    windows(core, [0] * 10 + [1] * 5)

    assert core.handle(Stop("stopped"), 0) == [End("stopped")]
    assert core.handle(Stop("disconnected"), 0) == []


def test_the_timeouts_come_from_the_timing() -> None:
    core = listening(no_speech_ms=500, listen_max_ms=900, turn_silence_ms=400)

    assert core.next_deadline() == S // 2
    windows(core, SPEECH)
    assert core.next_deadline() == 900_000
    # The speech ended at 15360; 400 ms later is 21760, 12.5 windows on from there.
    assert windows(core, [0] * 5) == []
    assert windows(core, [0]) == [End("end_of_turn")]


def test_a_held_listen_never_ends_its_turn_and_asks_no_verdict() -> None:
    core = listening()
    core.handle(Hold(), 0)
    said = segment(core, SPEECH)

    assert said.turn is False
    assert core.handle(TurnVerdict(said.index, complete=True), 0) == []
    assert windows(core, [0] * 100) == []


def test_a_hold_mid_listen_outdates_the_verdict_it_was_waiting_for() -> None:
    core = listening()
    said = segment(core, SPEECH)

    assert core.handle(Hold(), 0) == []
    assert core.handle(TurnVerdict(said.index, complete=True), 0) == []
    assert windows(core, [0] * 100) == []


def test_a_held_listen_has_no_no_speech_but_still_ends_at_60_s() -> None:
    core = listening()
    core.handle(Hold(), 0)

    assert core.next_deadline() == 60 * S
    assert core.handle(Tick(), 8 * S) == []
    assert core.handle(Tick(), 60 * S) == [End("max_duration")]


def test_a_release_ends_a_held_listen_with_its_last_segment() -> None:
    core = listening()
    core.handle(Hold(), 0)
    windows(core, [0] * 10 + [1] * 10)

    assert core.handle(Stop("button"), 0) == [
        Segment(0, start=320, end=10240, turn=False),
        End("button"),
    ]


def test_a_click_ends_a_held_listen_and_discards_the_speech_going_on() -> None:
    core = listening()
    core.handle(Hold(), 0)
    windows(core, [0] * 10 + [1] * 10)

    assert core.handle(Stop("click"), 0) == [End("click")]
