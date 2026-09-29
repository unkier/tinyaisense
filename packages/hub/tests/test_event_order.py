"""The event order core: one Connection's Events, each copy deduped by `seq` and put in `seq`
order, with raw button edges let through at once. Driven with a fake `now` in µs."""

from tinyaisense_hub.events import EventOrder, Ordered, Raw
from tinyaisense_hub.keepalive import Tick
from tinyaisense_wire import Event

MS = 1000
"""One millisecond in µs."""


def order() -> EventOrder:
    return EventOrder(hold=30 * MS, buttons={"button"})


def drained(seq: int) -> Event:
    return Event(seq, "speaker", "drained", seq * MS, {"stream": 1})


def test_events_in_seq_order_go_on_at_once() -> None:
    core = order()

    assert core.handle(drained(0), 0) == [Ordered(drained(0))]
    assert core.handle(drained(1), 0) == [Ordered(drained(1))]


def test_a_later_copy_of_an_event_is_dropped() -> None:
    core = order()
    core.handle(drained(0), 0)

    assert core.handle(drained(0), MS) == []


def button(seq: int, name: str) -> Event:
    return Event(seq, "button", name, seq * MS, {})


def test_a_raw_button_edge_goes_on_raw_and_in_order_once() -> None:
    core = order()

    assert core.handle(button(0, "down"), 0) == [Raw(button(0, "down")), Ordered(button(0, "down"))]
    assert core.handle(button(0, "down"), 5 * MS) == []


def test_an_event_ahead_of_a_gap_waits_for_it() -> None:
    core = order()

    assert core.handle(drained(1), 0) == []
    assert core.handle(drained(1), MS) == []
    assert core.handle(drained(0), 2 * MS) == [Ordered(drained(0)), Ordered(drained(1))]
    assert core.next_deadline() is None


def test_a_raw_button_edge_ahead_of_a_gap_goes_on_at_once_and_in_order_later() -> None:
    core = order()

    assert core.handle(button(1, "down"), 0) == [Raw(button(1, "down"))]
    assert core.handle(button(1, "down"), 5 * MS) == []
    assert core.handle(drained(0), 6 * MS) == [Ordered(drained(0)), Ordered(button(1, "down"))]


def test_the_hold_gives_up_on_a_gap_after_30_ms() -> None:
    core = order()
    core.handle(drained(1), 10 * MS)
    core.handle(drained(2), 20 * MS)

    assert core.next_deadline() == 40 * MS
    assert core.handle(Tick(), 40 * MS - 1) == []
    assert core.handle(Tick(), 40 * MS) == [Ordered(drained(1)), Ordered(drained(2))]
    assert core.next_deadline() is None
    assert core.handle(drained(3), 41 * MS) == [Ordered(drained(3))]


def test_an_event_from_a_gap_given_up_on_goes_on_when_it_comes_in_once() -> None:
    core = order()
    core.handle(drained(1), 0)
    core.handle(Tick(), 30 * MS)

    assert core.handle(drained(0), 200 * MS) == [Ordered(drained(0))]
    assert core.handle(drained(0), 201 * MS) == []


def test_a_late_raw_button_edge_goes_on_raw_too() -> None:
    core = order()
    core.handle(drained(1), 0)
    core.handle(Tick(), 30 * MS)

    assert core.handle(button(0, "up"), 200 * MS) == [
        Raw(button(0, "up")),
        Ordered(button(0, "up")),
    ]


def test_giving_up_on_one_gap_leaves_a_later_one_its_own_30_ms() -> None:
    core = order()
    core.handle(drained(1), 0)
    core.handle(drained(3), 20 * MS)

    assert core.handle(Tick(), 30 * MS) == [Ordered(drained(1))]
    assert core.next_deadline() == 50 * MS
    assert core.handle(drained(2), 40 * MS) == [Ordered(drained(2)), Ordered(drained(3))]
    assert core.next_deadline() is None
