"""The speaker's volume curve: docs/protocol/kinds/speaker.md#set_volume-hub-only."""

import math

import pytest
from tinyaisense_sim.speaker import volume_gain


def db(gain: float) -> float:
    return 20 * math.log10(gain)


def test_level_0_mutes() -> None:
    assert volume_gain(0) == 0.0


def test_level_1_is_about_minus_40_db_and_level_100_is_0_db() -> None:
    assert db(volume_gain(1)) == pytest.approx(-40)
    assert volume_gain(100) == 1.0


def test_each_level_step_is_the_same_number_of_db() -> None:
    steps = [db(volume_gain(level + 1)) - db(volume_gain(level)) for level in range(1, 100)]

    assert steps == pytest.approx([40 / 99] * 99)
