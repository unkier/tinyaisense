"""The speaker pacer core, driven with a fake `now` in µs."""

from tinyaisense_hub.keepalive import Tick
from tinyaisense_hub.pacer import Close, Drain, Feed, Open, Overflow, Pacer, Remove, Send

MS = 1000
"""One millisecond in µs."""
FRAME = 320
"""10 ms of 16 kHz mono s16le."""


def pcm(frames: float) -> bytes:
    """`frames` frames' worth of PCM, every byte telling its position apart."""
    return bytes(i % 251 for i in range(round(frames * FRAME)))


def pacer(cap_ms: int = 5000) -> Pacer[str]:
    core: Pacer[str] = Pacer(cap=cap_ms * MS)
    core.handle(Open("a", FRAME), 0)
    return core


def seqs(actions: list[Send[str] | Drain[str] | Overflow[str]]) -> list[int]:
    return [a.seq for a in actions if isinstance(a, Send)]


def test_the_first_60_ms_go_out_as_a_burst_as_soon_as_audio_arrives() -> None:
    core = pacer()

    sent = core.handle(Feed("a", pcm(100)), 3 * MS)

    assert sent == [Send("a", seq, pcm(100)[seq * FRAME : (seq + 1) * FRAME]) for seq in range(6)]


def test_then_each_tick_keeps_a_60_ms_lead_on_the_schedule_anchored_at_the_start() -> None:
    core = pacer()
    core.handle(Feed("a", pcm(100)), 3 * MS)

    assert core.next_deadline() == 13 * MS
    assert seqs(core.handle(Tick(), 13 * MS)) == [6]  # frame 6 plays at 63 ms
    assert core.next_deadline() == 23 * MS
    assert seqs(core.handle(Tick(), 23 * MS)) == [7]
    # A late tick catches up with the schedule, not with the ticks.
    assert seqs(core.handle(Tick(), 58 * MS)) == [8, 9, 10, 11]


def test_audio_that_arrives_on_schedule_keeps_the_anchor() -> None:
    core = pacer()
    core.handle(Feed("a", pcm(3)), 0)

    assert seqs(core.handle(Feed("a", pcm(10)), 5 * MS)) == [3, 4, 5, 6]
    assert seqs(core.handle(Tick(), 15 * MS)) == [7]


def test_a_source_that_runs_dry_gets_a_fresh_burst_when_audio_comes_back() -> None:
    core = pacer()
    core.handle(Feed("a", pcm(8)), 0)
    core.handle(Tick(), 10 * MS)
    core.handle(Tick(), 20 * MS)
    assert core.next_deadline() is None  # nothing left to send

    # Frame 8 was due to play at 80 ms: the device has run dry by 500 ms.
    assert seqs(core.handle(Feed("a", pcm(20)), 500 * MS)) == list(range(8, 14))
    assert seqs(core.handle(Tick(), 510 * MS)) == [14]


def test_audio_is_re_chunked_into_whole_frames_whatever_its_pieces() -> None:
    core = pacer()
    audio = pcm(4.5)

    sent = [core.handle(Feed("a", audio[i : i + 100]), 0) for i in range(0, len(audio), 100)]

    frames = [a for actions in sent for a in actions if isinstance(a, Send)]
    assert [f.seq for f in frames] == [0, 1, 2, 3]
    assert b"".join(f.payload for f in frames) == audio[: 4 * FRAME]


def test_a_closed_stream_sends_its_short_final_frame_then_drains() -> None:
    core = pacer()
    core.handle(Feed("a", pcm(1.5)), 0)

    assert core.handle(Close("a"), 1 * MS) == [Send("a", 1, pcm(1.5)[FRAME:]), Drain("a", 1)]


def test_a_closed_stream_drains_only_once_it_is_fully_sent() -> None:
    core = pacer()
    core.handle(Feed("a", pcm(8)), 0)

    assert core.handle(Close("a"), 0) == []
    assert seqs(core.handle(Tick(), 10 * MS)) == [6]
    assert core.handle(Tick(), 20 * MS) == [Send("a", 7, pcm(8)[7 * FRAME :]), Drain("a", 7)]
    assert core.next_deadline() is None


def test_a_stream_closed_without_audio_drains_at_once_with_no_last_seq() -> None:
    core = pacer()

    assert core.handle(Close("a"), 0) == [Drain("a", None)]


def test_audio_after_the_close_is_dropped() -> None:
    core = pacer()
    core.handle(Feed("a", pcm(1)), 0)
    core.handle(Close("a"), 0)

    assert core.handle(Feed("a", pcm(1)), 0) == []
    assert core.next_deadline() is None


def test_unsent_audio_over_the_cap_overflows_and_the_stream_is_forgotten() -> None:
    core = pacer(cap_ms=1000)
    # The burst goes out, and exactly 1 s is left unsent: at the cap, not over it.
    assert seqs(core.handle(Feed("a", pcm(106)), 0)) == list(range(6))

    assert core.handle(Feed("a", b"\0\0"), 0) == [Overflow("a")]
    assert core.handle(Tick(), 10 * MS) == []
    assert core.next_deadline() is None


def test_a_removed_stream_sends_nothing_more() -> None:
    core = pacer()
    core.handle(Feed("a", pcm(100)), 0)

    assert core.handle(Remove("a"), 5 * MS) == []
    assert core.handle(Tick(), 10 * MS) == []
    assert core.next_deadline() is None


def test_streams_share_the_ticks_and_each_keeps_its_own_schedule() -> None:
    core = pacer()
    core.handle(Open("b", FRAME), 0)
    core.handle(Feed("a", pcm(100)), 0)
    core.handle(Tick(), 10 * MS)

    assert seqs(core.handle(Feed("b", pcm(100)), 12 * MS)) == list(range(6))
    assert core.next_deadline() == 20 * MS
    assert core.handle(Tick(), 20 * MS) == [
        Send("a", 7, pcm(100)[7 * FRAME : 8 * FRAME]),
        Send("b", 6, pcm(100)[6 * FRAME : 7 * FRAME]),
    ]
