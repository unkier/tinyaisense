"""The bench's gates, each a latency percentile against a target, and the word error rate of
transcripts, which is reported, not gated."""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..stats import nearest_rank


def percentile(samples: Sequence[float], p: float) -> float:
    """The `p`th percentile of `samples`, in ms, by nearest rank. A lost sample is `math.inf`:
    the slowest. With no samples, it's `math.inf` too."""
    value = nearest_rank(sorted(samples), p / 100)
    return math.inf if value is None else value


@dataclass(slots=True)
class Gate:
    """A latency target, `p`th percentile at most `ms`, and the samples of each run against it.

    The flake policy: a run misses if its percentile is over the target, and the gate fails
    only if most runs miss, e.g. 2 of 3. With no runs, it fails: nothing was shown."""

    key: str
    title: str
    p: int
    ms: float
    runs: list[list[float]] = field(default_factory=list[list[float]])
    """Each run's samples, in ms."""

    def value(self, run: int) -> float:
        return percentile(self.runs[run], self.p)

    def missed(self, run: int) -> bool:
        return self.value(run) > self.ms

    @property
    def misses(self) -> int:
        return sum(self.missed(run) for run in range(len(self.runs)))

    @property
    def passed(self) -> bool:
        return bool(self.runs) and 2 * self.misses <= len(self.runs)


_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")
"""A word: letters and digits, with apostrophes inside it."""
_SAME = str.maketrans({"ё": "е", "\u2019": "'"})  # noqa: RUF001
"""What reads the same: `ё` as `е`, and a typographic apostrophe as a plain one."""  # noqa: RUF001


def words(text: str) -> list[str]:
    """`text`'s words, to compare: in lower case, with no punctuation, `ё` read as `е` and a
    typographic apostrophe as a plain one."""  # noqa: RUF002
    return _WORD.findall(text.lower().translate(_SAME))


def word_errors(reference: str, heard: str) -> tuple[int, int]:
    """How many words `heard` got wrong, substituted, inserted or deleted, against
    `reference`; and how many words `reference` has."""
    said, got = words(reference), words(heard)
    row = list(range(len(got) + 1))
    for i, word in enumerate(said, 1):
        previous, row[0] = row[0], i
        for j, other in enumerate(got, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (word != other))
    return row[-1], len(said)
