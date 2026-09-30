"""The bench's gates: a latency percentile against its target, judged over runs, and the word
error rate of transcripts."""

import math

import pytest
from tinyaisense_hub.bench.gates import Gate, percentile, word_errors


def test_a_percentile_is_the_sample_at_its_nearest_rank() -> None:
    samples = [float(ms) for ms in range(100, 0, -1)]

    assert percentile(samples, 95) == 95.0
    assert percentile(samples, 99) == 99.0
    assert percentile([7.0], 99) == 7.0


def test_a_lost_sample_counts_as_the_slowest() -> None:
    assert percentile([1.0] * 98 + [math.inf] * 2, 99) == math.inf


def test_a_percentile_of_nothing_is_infinite() -> None:
    assert percentile([], 95) == math.inf


def gate(*runs: list[float]) -> Gate:
    return Gate("stt", "STT", 95, 150, [list(run) for run in runs])


def test_a_run_misses_when_its_percentile_is_over_the_target() -> None:
    passing = [100.0] * 19 + [900.0]
    missing = [100.0] * 18 + [900.0] * 2

    assert not gate(passing).missed(0)
    assert gate(missing).missed(0)
    assert not gate([150.0]).missed(0)


def test_a_gate_fails_only_if_two_of_three_runs_miss() -> None:
    ok, miss = [100.0], [200.0]

    assert gate(ok, ok, ok).passed
    assert gate(miss, ok, ok).passed
    assert not gate(miss, ok, miss).passed
    assert not gate(miss, miss, miss).passed


def test_a_gate_run_once_fails_if_that_run_misses() -> None:
    assert gate([100.0]).passed
    assert not gate([200.0]).passed


def test_a_gate_never_run_fails() -> None:
    assert not gate().passed


def test_word_errors_count_substitutions_insertions_and_deletions() -> None:
    assert word_errors("the cat sat on the mat", "the cat sat on the mat") == (0, 6)
    assert word_errors("the cat sat on the mat", "the dog sat on mat") == (2, 6)
    assert word_errors("the cat sat", "the cat sat down") == (1, 3)


def test_word_errors_ignore_case_and_punctuation() -> None:
    assert word_errors("EITHER HE CALLS MINISTERS", "Either he calls ministers.") == (0, 4)
    assert word_errors("It's late, isn't it?", "its late isn't it") == (1, 4)


def test_word_errors_read_a_typographic_apostrophe_as_a_plain_one() -> None:
    assert word_errors("I'M NOWHERE", "I\u2019m nowhere.") == (0, 2)


def test_word_errors_read_yo_as_ye_in_russian() -> None:
    assert word_errors("Ещё немного, всё!", "еще немного все") == (0, 3)


def test_word_errors_of_nothing_heard_are_every_word() -> None:
    assert word_errors("the cat sat", "") == (3, 3)


@pytest.mark.parametrize("hyphenated", ["финансово-экономического", "well-known"])
def test_a_hyphenated_compound_is_its_words(hyphenated: str) -> None:
    assert word_errors(hyphenated, hyphenated.replace("-", " ")) == (0, 2)
