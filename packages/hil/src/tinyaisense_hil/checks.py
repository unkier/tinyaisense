"""What the HIL tier checks its numbers by. Each check returns its findings, what's wrong, so that
a report can show them all: none means it passed.

- The interrupt samples, against the hub's own `stats` and against the serial line.
- The soak's diag, against its invariants.
"""

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from tinyaisense_hub.bench.gates import percentile
from tinyaisense_wire import JsonObject

P99_AGREE_MS = 2.0
"""How far the agent's p99 may be from the hub's: the agent's samples add the SDK's read, and
`t`'s whole ms."""
CLOCK_SLACK_MS = 3.0
"""How much earlier than its serial write the hub may put an edge, by its clock sync with the
Echo, before the samples can't be trusted."""


@dataclass(frozen=True, slots=True)
class Edge:
    """One injected button edge, in epoch ms: when its `btn` went out on the serial line; when
    it happened on the Echo, the `t` of its notification, by the hub's clock sync; and when the
    SDK got it. A lost edge has no `t` and wasn't `got`."""

    edge: Literal["down", "up"]
    sent: float
    t: int | None
    got: float | None

    @property
    def ms(self) -> float:
        """From the edge on the Echo to the agent: the Interrupt gate's sample."""
        return math.inf if self.t is None or self.got is None else self.got - self.t

    @property
    def bound_ms(self) -> float:
        """From the serial write to the agent, which takes longer than the edge: by the serial
        line, and the console."""
        return math.inf if self.got is None else self.got - self.sent


def interrupt_findings(edges: Sequence[Edge], latency: JsonObject) -> list[str]:
    """Check the agent's samples of `edges` against the hub's `stats` of the same edges,
    `button_latency`: the hub must have counted each edge the agent got, its p99 must agree, and
    the hub mustn't put an edge well before its serial write."""
    findings: list[str] = []
    got = [edge for edge in edges if edge.got is not None]
    if latency["count"] != len(got):
        findings.append(f"the hub counted {latency['count']} edges; the agent got {len(got)}")
    ours, hubs = percentile([edge.ms for edge in got], 99), cast(float | None, latency["p99_ms"])
    if hubs is None or abs(ours - hubs) > P99_AGREE_MS:
        findings.append(f"the agent's p99 is {ours:.1f} ms, the hub's {hubs} ms")
    early = max((edge.ms - edge.bound_ms for edge in got), default=0.0)
    if early > CLOCK_SLACK_MS:
        findings.append(
            f"the hub put an edge {early:.1f} ms before its serial write: its clock sync with "
            f"the Echo is off by that much at least"
        )
    return findings


HEAP_MIN_FLOOR = 40 * 1024
STACK_FLOOR = 512
WARM_UP = 0.25
"""The part of the soak heap_min may fall in, while its workload comes round a few times: after
it, it stays flat."""
FLAT_BYTES = 2048
"""How far heap_min may fall after the warm-up and still be flat: a few more buffers at once,
not a leak."""


@dataclass(frozen=True, slots=True)
class Diag:
    """The Echo's `diag`, `at_s` into the soak."""

    at_s: float
    uptime_s: int
    reset_reason: str
    heap_free: int
    heap_min: int
    reconnects: int
    stack_hwm: dict[str, int]

    @classmethod
    def from_json(cls, at_s: float, diag: JsonObject) -> Diag:
        return cls(
            at_s=at_s,
            uptime_s=cast(int, diag["uptime_s"]),
            reset_reason=cast(str, diag["reset_reason"]),
            heap_free=cast(int, diag["heap_free"]),
            heap_min=cast(int, diag["heap_min"]),
            reconnects=cast(int, diag["reconnects"]),
            stack_hwm=cast(dict[str, int], diag["stack_hwm"]),
        )


def soak_findings(diags: Sequence[Diag]) -> list[str]:
    """The soak's invariants, over its `diags` from start to end: heap_min flat after the
    warm-up and 40 KB or more; no reset besides the power-on before it; every task over 512 B
    of stack to spare; and no reconnects."""
    first, last = diags[0], diags[-1]
    findings: list[str] = []
    lowest = min(diags, key=lambda d: d.heap_min)
    if lowest.heap_min < HEAP_MIN_FLOOR:
        findings.append(
            f"heap_min fell to {lowest.heap_min} B, under 40 KB, at {lowest.at_s:.0f} s"
        )
    warm = next(d for d in diags if d.at_s >= WARM_UP * last.at_s)
    if warm.heap_min - last.heap_min > FLAT_BYTES:
        findings.append(
            f"heap_min isn't flat: it fell from {warm.heap_min} B at {warm.at_s:.0f} s, after "
            f"the warm-up, to {last.heap_min} B at the end"
        )
    tight = [(task, bytes_, d) for d in diags for task, bytes_ in d.stack_hwm.items()]
    task, bytes_, at = min(tight, key=lambda t: t[1], default=("", STACK_FLOOR + 1, first))
    if bytes_ <= STACK_FLOOR:
        findings.append(f"{task} had only {bytes_} B of stack to spare, at {at.at_s:.0f} s")
    if reasons := sorted({d.reset_reason for d in diags} - {"poweron"}):
        findings.append(f"the Echo last reset by {', '.join(reasons)}, not by power-on")
    for before, after in itertools.pairwise(diags):
        if after.uptime_s < before.uptime_s:
            findings.append(f"the Echo reset between {before.at_s:.0f} s and {after.at_s:.0f} s")
    if reconnects := last.reconnects - first.reconnects:
        findings.append(f"the Echo counted {reconnects} reconnects")
    return findings
