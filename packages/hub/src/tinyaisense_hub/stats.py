"""The hub's always-on stats, for the owner's `stats`."""

import math
from collections import deque

from tinyaisense_wire import JsonObject

BUCKETS_MS = (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 75, 100, 150, 200, 300, 500)
"""The histogram's buckets, by their upper bound. One more takes everything slower."""
KEPT = 10_000
"""How many of the latest samples the histogram covers."""


class Latency:
    """A latency histogram over the latest `KEPT` samples."""

    def __init__(self) -> None:
        self._samples: deque[int] = deque(maxlen=KEPT)
        """In µs."""

    def record(self, us: int) -> None:
        """One sample, in µs. A negative one, from clock sync error, counts as 0."""
        self._samples.append(max(0, us))

    def to_json(self) -> JsonObject:
        """`count`; `p50_ms`, `p99_ms` and `max_ms`, None with no samples; and `buckets`: the
        count of samples in each bucket, `{le_ms, count}`, over the one below it. The last
        bucket's `le_ms` is None."""
        samples = sorted(self._samples)
        counts = [0] * (len(BUCKETS_MS) + 1)
        bucket = 0
        for us in samples:
            while bucket < len(BUCKETS_MS) and us > BUCKETS_MS[bucket] * 1000:
                bucket += 1
            counts[bucket] += 1
        bounds: list[int | None] = [*BUCKETS_MS, None]
        return {
            "count": len(samples),
            "p50_ms": _ms(_percentile(samples, 0.5)),
            "p99_ms": _ms(_percentile(samples, 0.99)),
            "max_ms": _ms(samples[-1] if samples else None),
            "buckets": [
                {"le_ms": le, "count": count} for le, count in zip(bounds, counts, strict=True)
            ],
        }


def _percentile(samples: list[int], p: float) -> int | None:
    """By nearest rank, of sorted `samples`."""
    return samples[max(0, math.ceil(p * len(samples)) - 1)] if samples else None


def _ms(us: int | None) -> float | None:
    return None if us is None else round(us / 1000, 1)
