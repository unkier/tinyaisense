"""The mic assembler core, driven with a fake `now` in µs."""

from tinyaisense_hub.assembler import Deliver, Finished, Frame, Last, MicAssembler
from tinyaisense_hub.keepalive import Tick
from tinyaisense_wire import AudioFormat

MS = 1000
"""One millisecond in µs."""
FORMAT = AudioFormat("pcm_s16le", 16000, 1, 10)
SILENCE = bytes(320)


def pcm(seq: int, size: int = 320) -> bytes:
    """A frame of `size` bytes that tells its seq apart."""
    return bytes([seq % 250 + 1]) * size


def assembler() -> MicAssembler:
    return MicAssembler(FORMAT, tail=30 * MS)


def delivered(actions: list[Deliver | Finished]) -> list[tuple[int, bytes]]:
    return [(a.index, a.pcm) for a in actions if isinstance(a, Deliver)]


def test_frames_in_order_are_delivered_at_once_on_their_seq() -> None:
    core = assembler()

    assert core.handle(Frame(0, pcm(0)), 0) == [Deliver(0, pcm(0))]
    assert core.handle(Frame(1, pcm(1)), 10 * MS) == [Deliver(1, pcm(1))]
    assert core.frames == 2


def test_a_gap_is_zero_filled_as_soon_as_a_later_frame_arrives() -> None:
    core = assembler()
    core.handle(Frame(0, pcm(0)), 0)

    actions = core.handle(Frame(3, pcm(3)), 30 * MS)

    assert delivered(actions) == [(1, SILENCE), (2, SILENCE), (3, pcm(3))]
    assert (core.frames, core.filled) == (4, 2)


def test_a_late_frame_or_a_duplicate_is_dropped() -> None:
    core = assembler()
    core.handle(Frame(0, pcm(0)), 0)
    core.handle(Frame(2, pcm(2)), 20 * MS)

    assert core.handle(Frame(1, pcm(1)), 21 * MS) == []  # late: already zero-filled
    assert core.handle(Frame(2, pcm(2)), 22 * MS) == []  # a duplicate
    assert core.late == 2
    assert core.frames == 3


def test_a_frame_that_breaks_the_frame_rules_is_dropped_and_counted_by_why() -> None:
    core = assembler()

    assert core.handle(Frame(0, bytes(319)), 0) == []
    assert core.handle(Frame(0, bytes(322)), 0) == []
    assert core.handle(Frame(0, b""), 0) == []
    assert core.rejected == {"partial_sample": 1, "oversized_frame": 1, "short_frame": 1}
    assert core.frames == 0


def test_a_short_frame_waits_for_the_final_seq_and_is_delivered_padded_if_it_is_the_last() -> None:
    core = assembler()
    core.handle(Frame(0, pcm(0)), 0)

    assert core.handle(Frame(1, pcm(1, 100)), 10 * MS) == []

    # Padded with silence: every frame the agent gets is 10 ms.
    assert core.handle(Last(1), 12 * MS) == [Deliver(1, pcm(1, 100) + bytes(220)), Finished()]


def test_a_held_short_frame_that_isnt_the_last_is_dropped_as_short() -> None:
    core = assembler()
    core.handle(Frame(0, pcm(0)), 0)
    core.handle(Frame(1, pcm(1, 100)), 10 * MS)

    actions = core.handle(Frame(2, pcm(2)), 20 * MS)

    assert delivered(actions) == [(1, SILENCE), (2, pcm(2))]
    assert core.rejected == {"short_frame": 1}


def test_a_short_frame_that_isnt_the_last_known_seq_is_dropped_as_short() -> None:
    core = assembler()
    core.handle(Frame(0, pcm(0)), 0)
    core.handle(Last(3), 5 * MS)

    assert core.handle(Frame(1, pcm(1, 100)), 6 * MS) == []
    assert core.rejected == {"short_frame": 1}


def test_the_stop_reply_finishes_at_once_when_every_frame_is_in() -> None:
    core = assembler()
    core.handle(Frame(0, pcm(0)), 0)
    core.handle(Frame(1, pcm(1)), 10 * MS)

    assert core.handle(Last(1), 12 * MS) == [Finished()]
    assert core.next_deadline() is None


def test_after_the_stop_reply_missing_tail_frames_get_30_ms_then_are_zero_filled() -> None:
    core = assembler()
    core.handle(Frame(0, pcm(0)), 0)
    assert core.handle(Last(3), 12 * MS) == []
    assert core.next_deadline() == 42 * MS

    assert core.handle(Frame(1, pcm(1)), 20 * MS) == [Deliver(1, pcm(1))]
    assert core.handle(Tick(), 42 * MS) == [Deliver(2, SILENCE), Deliver(3, SILENCE), Finished()]
    assert (core.frames, core.filled) == (4, 2)


def test_a_tail_frame_that_makes_it_in_time_finishes_the_stream() -> None:
    core = assembler()
    core.handle(Frame(0, pcm(0)), 0)
    core.handle(Last(1), 5 * MS)

    assert core.handle(Frame(1, pcm(1)), 8 * MS) == [Deliver(1, pcm(1)), Finished()]
    assert core.next_deadline() is None


def test_a_stream_that_sent_nothing_finishes_at_once_on_a_null_last_seq() -> None:
    core = assembler()

    assert core.handle(Last(None), 0) == [Finished()]
    assert core.frames == 0


def test_nothing_is_delivered_once_finished() -> None:
    core = assembler()
    core.handle(Last(None), 0)

    assert core.handle(Frame(0, pcm(0)), 1 * MS) == []
    assert core.handle(Tick(), 50 * MS) == []


def test_a_frame_past_the_last_seq_is_dropped() -> None:
    core = assembler()
    core.handle(Frame(0, pcm(0)), 0)
    core.handle(Last(1), 5 * MS)

    assert core.handle(Frame(2, pcm(2)), 6 * MS) == []
    assert core.rejected == {"past_last": 1}


def test_a_frame_implausibly_far_ahead_is_dropped_instead_of_zero_filling_to_it() -> None:
    core = assembler()
    core.handle(Frame(0, pcm(0)), 0)

    assert core.handle(Frame(4_000_000_000, pcm(0)), 10 * MS) == []
    assert core.rejected == {"bad_seq": 1}
    assert core.handle(Frame(1, pcm(1)), 11 * MS) == [Deliver(1, pcm(1))]


def test_after_a_real_gap_of_over_a_minute_the_timeline_catches_up() -> None:
    core = assembler()
    core.handle(Frame(0, pcm(0)), 0)

    actions = core.handle(Frame(7000, pcm(7000)), 70_000 * MS)

    assert delivered(actions)[-1] == (7000, pcm(7000))
    assert (core.frames, core.filled) == (7001, 6999)


def test_a_last_seq_implausibly_far_ahead_finishes_the_stream_as_it_stands() -> None:
    core = assembler()
    core.handle(Frame(0, pcm(0)), 0)

    assert core.handle(Last(4_000_000_000), 10 * MS) == [Finished()]
    assert core.rejected == {"bad_seq": 1}
    assert core.frames == 1
