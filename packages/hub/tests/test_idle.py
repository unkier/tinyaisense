"""The idle release of an MCP-only Session, sans-IO: times are µs."""

from tinyaisense_hub.idle import CallEnded, CallStarted, Idle, Release
from tinyaisense_hub.keepalive import Tick

AFTER = 600_000_000


def test_a_session_is_released_once_no_call_has_run_for_the_idle_time() -> None:
    idle = Idle(after=AFTER)
    idle.handle(CallStarted(), 0)
    idle.handle(CallEnded(), 5_000_000)

    assert idle.next_deadline() == 5_000_000 + AFTER
    assert idle.handle(Tick(), 5_000_000 + AFTER - 1) == []
    assert idle.handle(Tick(), 5_000_000 + AFTER) == [Release()]
    assert idle.next_deadline() is None
    assert idle.handle(Tick(), 5_000_000 + 2 * AFTER) == []


def test_a_call_that_runs_longer_than_the_idle_time_keeps_it() -> None:
    idle = Idle(after=AFTER)
    idle.handle(CallStarted(), 0)

    assert idle.next_deadline() is None
    assert idle.handle(Tick(), 2 * AFTER) == []


def test_the_idle_time_counts_from_the_end_of_the_last_call_running() -> None:
    idle = Idle(after=AFTER)
    idle.handle(CallStarted(), 0)
    idle.handle(CallStarted(), 1)
    idle.handle(CallEnded(), 10)

    assert idle.next_deadline() is None

    idle.handle(CallEnded(), 20)
    idle.handle(CallStarted(), 30)
    idle.handle(CallEnded(), 40)

    assert idle.next_deadline() == 40 + AFTER
    assert idle.handle(Tick(), 20 + AFTER) == []
