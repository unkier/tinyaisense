"""What the HIL tier checks its numbers by. Each check returns its findings, what's wrong, so that
a report can show them all: none means it passed.

- The interrupt samples, against the hub's own `stats` and against the serial line.
- Each fault scenario, against its expected behaviour.
- The soak's diag, against its invariants.
"""

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
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


LOSS = {"loss2": 0.02, "loss5-bursts": 0.05, "jitter30": 0.0}
"""What each scenario loses of the datagrams, each way, on average."""
LOSS_SLACK = 0.01
"""What a stream may lose besides 3 times its scenario's loss: a 3 s stream's few bursts
vary a lot."""
PLAYED_SLACK_MS = 20


@dataclass(slots=True)
class FaultRun:
    """What one fault scenario got. Under `loss2`, `loss5-bursts` and `jitter30`: a speaker
    stream drained, a mic stream stopped, and presses while neither ran. Under `blackout`: a
    press while a speaker stream plays, and the network is out, then a speaker stream `after`
    the blackout."""

    scenario: str
    edges: list[Edge]
    """The presses' edges, in the order they were sent."""
    events: list[str]
    """The raw button events the agent got meanwhile, `down` or `up`, in order."""
    offline: int
    """`device.offline`s the agent got."""
    reconnects: int
    """The Echo's own count over the scenario, from `diag`."""
    speaker: JsonObject
    """The speaker stream's end, `{reason, stats}`."""
    speaker_ms: int
    """What it was sent."""
    mic: JsonObject | None
    """The mic stream's end, `{reason, stats}`."""
    mic_ms: int
    """How long it ran."""
    after: JsonObject | None = None
    after_ms: int = 0
    netem: str = field(default="", compare=False)
    """What the netem wrapper ran."""


def fault_findings(run: FaultRun) -> list[str]:
    """Each scenario keeps the Connection, and gets every press to the agent once, in order.
    Its streams play through, or deliver their whole timeline, and lose about what the network
    does. A press in a blackout cuts the speaker on the Echo itself, and the Echo plays again
    after it."""
    findings: list[str] = []
    if run.offline:
        findings.append(f"the Echo went offline {run.offline} times")
    if run.reconnects:
        findings.append(f"the Echo counted {run.reconnects} reconnects")
    if lost := sum(edge.got is None for edge in run.edges):
        findings.append(f"{lost} of {len(run.edges)} edges never reached the agent")
    if run.events != [edge.edge for edge in run.edges]:
        findings.append(f"the agent got {_runs(run.events)}, for {len(run.edges) // 2} presses")
    if run.scenario == "blackout":
        if run.speaker["reason"] != "reflex":
            findings.append(
                f"the press in the blackout didn't cut the speaker: it ended "
                f"{run.speaker['reason']}, not reflex"
            )
        if run.after is not None:
            findings += _played("the speaker stream after the blackout", run.after, run.after_ms)
        return findings
    findings += _played("the speaker stream", run.speaker, run.speaker_ms)
    findings += _lost("the speaker stream", run.speaker, run.speaker_ms, run.scenario)
    if run.mic is not None:
        stats = cast(JsonObject, run.mic["stats"])
        frames = cast(int, stats["frames"])
        if run.mic["reason"] != "stopped" or frames < 0.9 * run.mic_ms / 10:
            findings.append(
                f"the mic stream of {run.mic_ms} ms ended {run.mic['reason']} with {frames} "
                f"frames on its timeline"
            )
        findings += _lost("the mic stream", run.mic, frames * 10, run.scenario)
    return findings


def _played(what: str, ended: JsonObject, sent_ms: int) -> list[str]:
    played = cast(int, cast(JsonObject, ended["stats"])["played_ms"])
    if ended["reason"] == "drained" and played >= sent_ms - PLAYED_SLACK_MS:
        return []
    return [f"{what} ended {ended['reason']}, having played {played} of {sent_ms} ms"]


def _lost(what: str, ended: JsonObject, ms: int, scenario: str) -> list[str]:
    lost, frames = cast(int, cast(JsonObject, ended["stats"])["lost"]), max(1, ms // 10)
    allowed = 3 * LOSS[scenario] + LOSS_SLACK
    if lost / frames <= allowed:
        return []
    return [f"{what} lost {lost} of {frames} frames, over the {allowed:.0%} {scenario} allows"]


def _runs(events: Sequence[str]) -> str:
    """`events`, as runs of the same: `down x2, up`."""
    runs: list[list[object]] = []
    for event in events:
        if runs and runs[-1][0] == event:
            runs[-1][1] = cast(int, runs[-1][1]) + 1
        else:
            runs.append([event, 1])
    return ", ".join(f"{e} x{n}" if n != 1 else str(e) for e, n in runs) or "no button events"


HEAP_MIN_FLOOR = 40 * 1024
STACK_FLOOR = 512
WARM_UP = 0.25
"""The part of the soak heap_min may fall in, while every scenario and a forced reconnect
come round once: after it, it stays flat."""
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


def soak_findings(diags: Sequence[Diag], forced: int) -> list[str]:
    """The soak's invariants, over its `diags` from start to end: heap_min flat after the
    warm-up and 40 KB or more; no reset besides the power-on before it; every task over 512 B
    of stack to spare; and as many reconnects as were `forced`."""
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
    if (reconnects := last.reconnects - first.reconnects) != forced:
        findings.append(f"the Echo counted {reconnects} reconnects; {forced} were forced")
    return findings
