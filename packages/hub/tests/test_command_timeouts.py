"""The command-timeout core, driven with a fake `now` in µs."""

from tinyaisense_hub.commands import Answered, CommandTimeouts, Replied, Sent, TimedOut
from tinyaisense_hub.keepalive import Tick

MS = 1000
"""One millisecond in µs."""


def test_a_reply_within_the_timeout_is_answered() -> None:
    core = CommandTimeouts()
    core.handle(Sent(1, 1000 * MS), 0)

    assert core.handle(Replied(1), 999 * MS) == [Answered(1)]
    assert core.next_deadline() is None


def test_a_command_without_a_reply_times_out_at_its_deadline() -> None:
    core = CommandTimeouts()
    core.handle(Sent(1, 1000 * MS), 5 * MS)

    assert core.next_deadline() == 1005 * MS
    assert core.handle(Tick(), 1004 * MS) == []
    assert core.handle(Tick(), 1005 * MS) == [TimedOut(1)]
    assert core.next_deadline() is None


def test_a_late_reply_is_dropped() -> None:
    core = CommandTimeouts()
    core.handle(Sent(1, 1000 * MS), 0)
    core.handle(Tick(), 1000 * MS)

    assert core.handle(Replied(1), 1200 * MS) == []


def test_a_reply_past_the_deadline_times_out_even_before_the_tick() -> None:
    core = CommandTimeouts()
    core.handle(Sent(1, 1000 * MS), 0)

    assert core.handle(Replied(1), 1000 * MS) == [TimedOut(1)]
    assert core.handle(Tick(), 1001 * MS) == []


def test_a_reply_with_an_unknown_id_is_ignored() -> None:
    core = CommandTimeouts()
    core.handle(Sent(1, 1000 * MS), 0)

    assert core.handle(Replied(7), 10 * MS) == []
    assert core.next_deadline() == 1000 * MS


def test_pipelined_commands_each_keep_their_own_timeout_and_may_be_answered_out_of_order() -> None:
    core = CommandTimeouts()
    core.handle(Sent(1, 1000 * MS), 0)
    core.handle(Sent(2, 3000 * MS), 0)
    core.handle(Sent(3, 1000 * MS), 100 * MS)

    assert core.handle(Replied(3), 200 * MS) == [Answered(3)]
    assert core.next_deadline() == 1000 * MS
    assert core.handle(Tick(), 1000 * MS) == [TimedOut(1)]
    assert core.next_deadline() == 3000 * MS
    assert core.handle(Replied(2), 2000 * MS) == [Answered(2)]


def test_everything_due_times_out_on_one_tick() -> None:
    core = CommandTimeouts()
    core.handle(Sent(1, 1000 * MS), 0)
    core.handle(Sent(2, 1000 * MS), 10 * MS)

    assert core.handle(Tick(), 2000 * MS) == [TimedOut(1), TimedOut(2)]
