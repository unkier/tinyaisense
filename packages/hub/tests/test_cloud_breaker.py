"""The cloud engine's circuit breaker, driven by a fake `now` in µs: after 3 failures in a row
the cloud is skipped for `cloud_skip_ms`."""

from tinyaisense_hub.speech.cloud import Breaker

S = 1_000_000
"""One second in µs."""


def test_the_cloud_is_tried_until_it_fails_3_times_in_a_row() -> None:
    breaker = Breaker(skip_ms=60_000)

    for at in (0, 1, 2):
        assert breaker.closed(at * S)
        breaker.failed(at * S)

    assert not breaker.closed(3 * S)


def test_a_success_between_failures_starts_the_count_again() -> None:
    breaker = Breaker(skip_ms=60_000)

    breaker.failed(0)
    breaker.failed(1 * S)
    breaker.succeeded()
    breaker.failed(2 * S)
    breaker.failed(3 * S)

    assert breaker.closed(4 * S)


def test_the_cloud_is_skipped_for_60_s_from_the_third_failure() -> None:
    breaker = Breaker(skip_ms=60_000)
    for at in (0, 5, 10):
        breaker.failed(at * S)

    assert not breaker.closed(70 * S - 1)
    assert breaker.closed(70 * S)


def test_after_the_skip_one_more_failure_skips_it_again_and_one_success_ends_that() -> None:
    breaker = Breaker(skip_ms=60_000)
    for _ in range(3):
        breaker.failed(0)

    breaker.failed(60 * S)
    assert not breaker.closed(61 * S)
    assert breaker.closed(120 * S)

    breaker.succeeded()
    breaker.failed(121 * S)
    assert breaker.closed(122 * S)
