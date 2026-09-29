"""The `thinking` core: the hub layer of one device's Status while the agent thinks, driven
with a fake `now` in µs."""

from tinyaisense_hub.keepalive import Tick
from tinyaisense_hub.thinking import Delivered, Moved, Show, Thinking

S = 1_000_000
"""One second in µs."""


def test_an_utterance_delivered_shows_thinking_for_60_s() -> None:
    core = Thinking(timeout=60 * S)

    assert core.next_deadline() is None
    assert core.handle(Delivered(), S) == [Show("thinking")]
    assert core.next_deadline() == 61 * S
    assert core.handle(Tick(), 61 * S - 1) == []
    assert core.handle(Tick(), 61 * S) == [Show("agent")]
    assert core.next_deadline() is None


def test_the_next_speaker_start_or_listen_ends_thinking() -> None:
    core = Thinking(timeout=60 * S)
    core.handle(Delivered(), 0)

    assert core.handle(Moved(), S) == [Show("agent")]
    assert core.next_deadline() is None
    assert core.handle(Moved(), 2 * S) == []


def test_another_utterance_restarts_the_60_s() -> None:
    core = Thinking(timeout=60 * S)
    core.handle(Delivered(), 0)

    assert core.handle(Delivered(), 10 * S) == []
    assert core.next_deadline() == 70 * S
