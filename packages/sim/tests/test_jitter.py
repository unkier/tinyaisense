"""The speaker's elastic jitter buffer: docs/protocol/audio.md#speaker-stream.

The firmware's tas_jbuf has the same cases in firmware/test/test_jbuf.
"""

from tinyaisense_sim.jitter import JitterBuffer, Pulled
from tinyaisense_wire import AudioFormat

FORMAT = AudioFormat("pcm_s16le", 16000, 1, 10)
FRAME = 320


def frame(seq: int, size: int = FRAME) -> bytes:
    """Frame `seq`: a byte unique to it, so a pulled frame says which one played."""
    return bytes([seq % 256]) * size


def buffer(prefill_ms: int = 40) -> JitterBuffer:
    return JitterBuffer(FORMAT, prefill_ms=prefill_ms)


def push(jb: JitterBuffer, *seqs: int) -> None:
    for seq in seqs:
        assert jb.push(seq, frame(seq))


def play(seq: int, size: int = FRAME) -> Pulled:
    return Pulled("play", seq, frame(seq, size))


def lost(seq: int) -> Pulled:
    return Pulled("lost", seq, b"")


PREFILL = Pulled("prefill", None, b"")
UNDERRUN = Pulled("underrun", None, b"")
DONE = Pulled("done", None, b"")


def test_playback_waits_for_the_prefill() -> None:
    jb = buffer()
    push(jb, 0, 1, 2)
    assert jb.pull() == PREFILL

    push(jb, 3)
    assert jb.pull() == play(0)


def test_a_gap_with_later_frames_buffered_is_lost_and_skipped() -> None:
    jb = buffer()
    push(jb, 0, 2, 3, 4)

    assert [jb.pull() for _ in range(3)] == [play(0), lost(1), play(2)]
    assert (jb.stats.lost, jb.stats.played_ms, jb.stats.underruns) == (1, 30, 0)


def test_an_empty_buffer_pauses_without_skipping() -> None:
    jb = buffer()
    push(jb, 0, 1, 2, 3)
    assert [jb.pull() for _ in range(4)] == [play(0), play(1), play(2), play(3)]

    assert [jb.pull(), jb.pull()] == [UNDERRUN, UNDERRUN]
    push(jb, 4)
    assert [jb.pull(), jb.pull()] == [play(4), UNDERRUN]

    stats = jb.stats
    assert (stats.underruns, stats.underrun_ms, stats.played_ms, stats.lost) == (2, 30, 50, 0)


def test_after_drain_missing_frames_are_concealed_until_last_seq() -> None:
    jb = buffer()
    push(jb, 0, 1, 2, 3, 5)
    jb.drain(7)
    assert [jb.pull() for _ in range(4)] == [play(0), play(1), play(2), play(3)]

    assert [jb.pull(), jb.pull()] == [lost(4), play(5)]
    assert jb.pull() == lost(6)  # an empty buffer, but no pause after drain
    push(jb, 7)
    assert [jb.pull(), jb.pull(), jb.pull()] == [play(7), DONE, DONE]
    assert (jb.stats.played_ms, jb.stats.lost, jb.stats.underruns) == (80, 2, 0)


def test_drain_starts_a_stream_shorter_than_the_prefill() -> None:
    jb = buffer()
    push(jb, 0, 1)
    assert jb.pull() == PREFILL

    jb.drain(1)
    assert [jb.pull(), jb.pull(), jb.pull()] == [play(0), play(1), DONE]


def test_late_duplicate_and_trailing_frames_are_dropped() -> None:
    jb = buffer()
    push(jb, 0, 2, 3, 4)
    assert [jb.pull(), jb.pull()] == [play(0), lost(1)]

    assert not jb.push(0, frame(0))  # already played
    assert not jb.push(1, frame(1))  # already concealed
    assert not jb.push(3, frame(3))  # a duplicate
    jb.drain(5)
    assert not jb.push(6, frame(6))  # past last_seq
    assert [jb.pull(), jb.pull()] == [play(2), play(3)]


def test_overflow_drops_the_oldest_frames_and_skips_past_them() -> None:
    jb = buffer()
    push(jb, 0, 1, 2, 3)
    assert jb.pull() == play(0)
    push(jb, *range(4, 21))  # holds 1..20: 200 ms

    push(jb, 21)
    assert jb.pull() == play(2)
    assert (jb.stats.lost, jb.stats.played_ms) == (1, 30)


def test_an_overflow_skips_to_the_oldest_frame_held() -> None:
    jb = buffer()
    push(jb, 0, 1, 2, 3)
    assert jb.pull() == play(0)

    push(jb, 25)  # 200 ms past the playhead: 1-3 drop, and nothing is held before 25
    assert jb.pull() == play(25)
    assert (jb.stats.lost, jb.stats.played_ms) == (24, 260)


def test_playback_resumes_at_the_oldest_frame_held() -> None:
    jb = buffer()
    push(jb, 0, 1, 2, 3)
    assert [jb.pull() for _ in range(5)] == [play(0), play(1), play(2), play(3), UNDERRUN]

    push(jb, 6)  # 4 and 5 never came: the pause already stood in for them
    assert jb.pull() == play(6)
    assert (jb.stats.lost, jb.stats.played_ms) == (2, 70)


def test_without_prefill_playback_waits_for_the_first_frame() -> None:
    jb = buffer(prefill_ms=0)
    assert jb.pull() == PREFILL

    push(jb, 0)
    assert jb.pull() == play(0)
    assert jb.stats.underruns == 0


def test_a_prefill_beyond_the_capacity_is_capped() -> None:
    jb = buffer(prefill_ms=500)
    push(jb, *range(20))

    assert jb.pull() == play(0)


def test_a_short_final_frame_plays() -> None:
    jb = buffer()
    push(jb, 0, 1, 2, 3)
    assert jb.push(4, frame(4, 100))
    jb.drain(4)
    assert [jb.pull() for _ in range(4)] == [play(0), play(1), play(2), play(3)]

    assert [jb.pull(), jb.pull()] == [play(4, 100), DONE]
    assert jb.stats.played_ms == 43  # 40 ms and 50 samples


def test_a_short_frame_is_held_until_the_drain_says_it_is_final() -> None:
    jb = buffer()
    push(jb, 0, 1, 2, 3)
    assert jb.push(4, frame(4, 200))
    assert [jb.pull() for _ in range(5)] == [play(0), play(1), play(2), play(3), UNDERRUN]

    jb.drain(4)
    assert jb.pull() == play(4, 200)


def test_a_short_frame_that_is_not_the_final_one_is_dropped() -> None:
    jb = buffer()
    push(jb, 0, 1, 2, 3, 5)
    assert jb.push(4, frame(4, 100))
    assert jb.push(6, frame(6, 100))
    jb.drain(7)
    assert [jb.pull() for _ in range(4)] == [play(0), play(1), play(2), play(3)]

    assert [jb.pull(), jb.pull(), jb.pull()] == [lost(4), play(5), lost(6)]
    assert (jb.stats.short_frames, jb.stats.lost) == (2, 2)


def test_an_empty_or_oversized_frame_is_dropped() -> None:
    jb = buffer()

    assert not jb.push(0, b"")
    assert not jb.push(0, bytes(FRAME + 2))
    assert jb.push(0, bytes(FRAME))
