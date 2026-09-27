"""The keepalive, dead-detection and clock-sync cores, driven with a fake `now` in µs."""

from tinyaisense_hub.keepalive import ClockSync, Dead, Inbound, Keepalive, SendPing, Tick
from tinyaisense_wire import Pong

S = 1_000_000
"""One second in µs."""


def keepalive(now: int = 0) -> Keepalive:
    return Keepalive(now, ping_interval=2 * S, dead_after=6 * S)


def test_the_first_ping_goes_out_at_hello() -> None:
    core = keepalive(now=10 * S)

    assert core.next_deadline() == 10 * S
    assert core.handle(Tick(), 10 * S) == [SendPing(10 * S)]


def test_pings_go_out_every_interval() -> None:
    core = keepalive()
    core.handle(Tick(), 0)

    assert core.next_deadline() == 2 * S
    assert core.handle(Tick(), 2 * S) == [SendPing(2 * S)]
    assert core.next_deadline() == 4 * S


def test_silence_for_dead_after_marks_the_connection_dead() -> None:
    core = keepalive()
    core.handle(Tick(), 0)
    core.handle(Tick(), 2 * S)
    core.handle(Tick(), 4 * S)

    assert core.next_deadline() == 6 * S
    assert core.handle(Tick(), 6 * S) == [Dead()]
    assert core.next_deadline() is None
    assert core.handle(Tick(), 8 * S) == []


def test_any_inbound_frame_counts_as_liveness() -> None:
    core = keepalive()
    for now in (0, 2 * S, 4 * S):
        core.handle(Tick(), now)

    core.handle(Inbound(), 5 * S)

    for now in (6 * S, 8 * S, 10 * S):
        assert core.handle(Tick(), now) == [SendPing(now)]
    assert core.next_deadline() == 11 * S
    assert core.handle(Tick(), 11 * S) == [Dead()]


def test_a_late_tick_sends_one_ping_and_restarts_the_interval() -> None:
    core = keepalive()
    core.handle(Tick(), 0)
    core.handle(Inbound(), 5 * S)

    assert core.handle(Tick(), 5 * S) == [SendPing(5 * S)]
    assert core.next_deadline() == 7 * S


def test_an_early_tick_does_nothing() -> None:
    core = keepalive()
    core.handle(Tick(), 0)

    assert core.handle(Tick(), 2 * S - 1) == []
    assert core.next_deadline() == 2 * S


def clock(window: int = 20 * S) -> ClockSync:
    return ClockSync(window=window)


def test_the_clock_offset_is_unknown_before_any_pong() -> None:
    assert clock().offset is None


def test_a_pong_puts_the_device_clock_at_the_midpoint_of_the_round_trip() -> None:
    sync = clock()

    # Pinged at hub 100 ms, answered at device 1 s, back at hub 104 ms.
    sync.handle(Pong(t=100_000, ts=1_000_000), now=104_000)

    assert sync.offset == 1_000_000 - 102_000
    assert sync.to_hub(1_500_000) == 602_000


def test_the_pong_with_the_shortest_round_trip_wins() -> None:
    sync = clock()
    sync.handle(Pong(t=0, ts=1_000_000), now=2_000)  # rtt 2 ms: offset 999 ms
    sync.handle(Pong(t=2 * S, ts=3_050_000), now=2 * S + 40_000)  # rtt 40 ms: offset 1030 ms

    assert sync.offset == 999_000


def test_pongs_older_than_the_window_are_forgotten() -> None:
    sync = clock(window=4 * S)
    sync.handle(Pong(t=0, ts=1_000_000), now=2_000)  # rtt 2 ms
    sync.handle(Pong(t=2 * S, ts=3_050_000), now=2 * S + 40_000)  # rtt 40 ms

    sync.handle(Pong(t=4 * S, ts=5_060_000), now=4 * S + 30_000)  # rtt 30 ms

    assert sync.offset == 1_045_000


def test_a_pong_from_the_future_is_ignored() -> None:
    sync = clock()

    sync.handle(Pong(t=10 * S, ts=1), now=S)

    assert sync.offset is None
