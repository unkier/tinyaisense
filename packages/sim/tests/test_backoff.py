"""The reconnect backoff, with a seeded RNG and no clock."""

import random

import pytest
from tinyaisense_sim.backoff import Backoff

STEPS = [0.5, 1.0, 2.0, 5.0, 5.0, 5.0, 5.0]
"""docs/protocol/device.md: 0.5 → 1 → 2 → 5 s, the cap."""


def test_each_failure_in_a_row_waits_the_next_step_plus_up_to_20_percent() -> None:
    backoff = Backoff(random.Random(1))

    delays = [backoff.failed().delay for _ in STEPS]

    for delay, step in zip(delays, STEPS, strict=True):
        assert step <= delay <= step * 1.2
    assert delays != STEPS


def test_the_jitter_comes_from_the_seed() -> None:
    def delays(seed: int) -> list[float]:
        backoff = Backoff(random.Random(seed))
        return [backoff.failed().delay for _ in STEPS]

    assert delays(7) == delays(7)
    assert delays(7) != delays(8)


def test_a_welcome_resets_the_backoff() -> None:
    backoff = Backoff(random.Random(1))
    for _ in range(4):
        backoff.failed()

    backoff.welcomed()

    assert backoff.failed().delay == pytest.approx(0.5, rel=0.2)


def test_mdns_is_resolved_again_after_every_third_failure_in_a_row() -> None:
    backoff = Backoff(random.Random(1))

    assert [backoff.failed().resolve for _ in STEPS] == [False, False, True] * 2 + [False]

    backoff.welcomed()
    assert [backoff.failed().resolve for _ in range(3)] == [False, False, True]
