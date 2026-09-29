"""The gestures core: `click`, `double_click` and `long_press` from one button's raw edges,
driven with a fake `now` in µs."""

from tinyaisense_hub.gestures import Down, Gesture, Gestures, Up
from tinyaisense_hub.keepalive import Tick

MS = 1000
"""One millisecond in µs."""


def gestures() -> Gestures:
    return Gestures(click=250 * MS, double_click=300 * MS, long_press=1000 * MS)


def test_a_press_under_250_ms_is_a_click_as_it_ends() -> None:
    core = gestures()

    assert core.handle(Down(0), 0) == []
    assert core.handle(Up(249 * MS), 250 * MS) == [Gesture("click", 249 * MS)]


def test_a_press_of_250_ms_or_more_is_no_click() -> None:
    core = gestures()
    core.handle(Down(0), 0)

    assert core.handle(Up(250 * MS), 251 * MS) == []


def test_a_press_held_for_1_s_is_a_long_press_while_it_is_held() -> None:
    core = gestures()
    core.handle(Down(10 * MS), 12 * MS)

    assert core.next_deadline() == 1010 * MS
    assert core.handle(Tick(), 1010 * MS - 1) == []
    assert core.handle(Tick(), 1010 * MS) == [Gesture("long_press", 1010 * MS)]
    assert core.next_deadline() is None
    assert core.handle(Up(1500 * MS), 1501 * MS) == []


def test_a_release_after_1_s_that_beats_the_deadline_is_still_a_long_press() -> None:
    core = gestures()
    core.handle(Down(0), 0)

    # The loop was late: the release came in before the deadline's tick.
    assert core.handle(Up(1200 * MS), 1201 * MS) == [Gesture("long_press", 1000 * MS)]
    assert core.next_deadline() is None


def test_a_second_click_that_starts_within_300_ms_is_also_a_double_click() -> None:
    core = gestures()
    core.handle(Down(0), 0)
    core.handle(Up(100 * MS), 100 * MS)

    assert core.handle(Down(399 * MS), 399 * MS) == []
    assert core.handle(Up(500 * MS), 500 * MS) == [
        Gesture("click", 500 * MS),
        Gesture("double_click", 500 * MS),
    ]


def test_a_second_click_that_starts_300_ms_or_more_after_is_only_a_click() -> None:
    core = gestures()
    core.handle(Down(0), 0)
    core.handle(Up(100 * MS), 100 * MS)
    core.handle(Down(400 * MS), 400 * MS)

    assert core.handle(Up(500 * MS), 500 * MS) == [Gesture("click", 500 * MS)]


def test_a_third_quick_click_starts_over() -> None:
    core = gestures()
    for at in (0, 200):
        core.handle(Down(at * MS), at * MS)
        core.handle(Up((at + 50) * MS), (at + 50) * MS)
    core.handle(Down(400 * MS), 400 * MS)

    assert core.handle(Up(450 * MS), 450 * MS) == [Gesture("click", 450 * MS)]


def test_a_slow_press_between_two_clicks_makes_no_double_click() -> None:
    core = gestures()
    core.handle(Down(0), 0)
    core.handle(Up(50 * MS), 50 * MS)
    core.handle(Down(100 * MS), 100 * MS)
    core.handle(Up(500 * MS), 500 * MS)
    core.handle(Down(600 * MS), 600 * MS)

    assert core.handle(Up(650 * MS), 650 * MS) == [Gesture("click", 650 * MS)]


def test_a_release_with_no_press_is_nothing() -> None:
    # E.g. the button was held as the device reconnected.
    assert gestures().handle(Up(50 * MS), 50 * MS) == []


def test_a_press_that_comes_in_after_its_release_is_nothing() -> None:
    # Its copies were lost or late past the reorder hold, so the release went ahead of it.
    core = gestures()
    core.handle(Up(2050 * MS), 2060 * MS)

    assert core.handle(Down(2000 * MS), 3100 * MS) == []
    assert core.next_deadline() is None
    assert core.handle(Tick(), 3100 * MS) == []
