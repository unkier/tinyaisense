"""The reconnect backoff of docs/protocol/device.md. Sans-IO."""

import random
from dataclasses import dataclass

STEPS_S = (0.5, 1.0, 2.0, 5.0)
"""One step per failure in a row, capped at the last."""
JITTER = 0.2
"""Each wait is its step plus up to this fraction more."""
RESOLVE_EVERY = 3
"""mDNS is resolved again after this many failures in a row, and every as many after."""


@dataclass(frozen=True, slots=True)
class Retry:
    delay: float
    """Seconds to wait before the next attempt."""
    resolve: bool
    """Whether to resolve the hub over mDNS again first."""


class Backoff:
    """Counts the Connections that ended since the last `welcome`: failures in a row."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._failures = 0

    def failed(self) -> Retry:
        """A Connection ended, or never got going. Returns how to retry."""
        step = STEPS_S[min(self._failures, len(STEPS_S) - 1)]
        self._failures += 1
        return Retry(step * (1 + self._rng.uniform(0, JITTER)), self._failures % RESOLVE_EVERY == 0)

    def welcomed(self) -> None:
        self._failures = 0
