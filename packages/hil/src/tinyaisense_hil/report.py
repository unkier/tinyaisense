"""The HIL tier's acceptance report, in Markdown and JSON, over whatever of it ran: the Interrupt
gate and the soak, with the firmware and hub versions, the rig and the
machine."""

import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

from tinyaisense_hub.bench.gates import Gate, percentile
from tinyaisense_wire import JsonObject

from .checks import Diag, Edge, interrupt_findings, soak_findings


@dataclass(slots=True)
class Interrupt:
    gate: Gate
    edges: list[list[Edge]]
    """Each run's."""
    latency: JsonObject
    """The hub's `stats` of every run's edges, `button_latency`."""

    @property
    def findings(self) -> list[str]:
        return interrupt_findings([edge for run in self.edges for edge in run], self.latency)

    @property
    def passed(self) -> bool:
        return self.gate.passed and not self.findings


@dataclass(slots=True)
class Soak:
    minutes: float
    cycles: int
    """Rounds of its workload."""
    diags: list[Diag]
    errors: list[str] = field(default_factory=list[str])
    """What went wrong in its workload."""

    @property
    def findings(self) -> list[str]:
        held = soak_findings(self.diags) if self.diags else ["no diag came in"]
        return [*held, *self.errors]

    @property
    def passed(self) -> bool:
        return not self.findings


@dataclass(slots=True)
class HilReport:
    date: datetime
    hub: JsonObject
    """`version`, and `commit` if known."""
    firmware: JsonObject
    """`version`, `elf` (its sha256's start) and whether it was `flashed` for this run."""
    rig: JsonObject
    machine: JsonObject
    """From `tinyaisense_hub.bench.report.machine()`, at the start."""
    interrupt: Interrupt | None = None
    soak: Soak | None = None
    loadavg_after: tuple[float, float, float] | None = None
    errors: list[str] = field(default_factory=list[str])
    """What broke off a part before it got to the end."""

    @property
    def name(self) -> str:
        commit = cast(str | None, self.hub.get("commit"))
        if commit is None:
            return "hil"
        head, dirty, _ = commit.partition("-dirty")
        return f"hil-{head[:7]}{dirty}"

    @property
    def passed(self) -> bool:
        return (
            not self.errors
            and (self.interrupt is None or self.interrupt.passed)
            and (self.soak is None or self.soak.passed)
        )

    def write(self, into: Path) -> list[Path]:
        """Write `<date>-<time>-<name>.md` and `.json` into `into`. Returns their paths."""
        into.mkdir(parents=True, exist_ok=True)
        stem = f"{self.date:%Y-%m-%d-%H%M}-{self.name}"
        md, js = into / f"{stem}.md", into / f"{stem}.json"
        md.write_text(self.markdown())
        js.write_text(json.dumps(self.to_json(), indent=2, ensure_ascii=False) + "\n")
        return [md, js]

    def to_json(self) -> JsonObject:
        return {
            "name": self.name,
            "date": self.date.isoformat(),
            "passed": self.passed,
            "hub": self.hub,
            "firmware": self.firmware,
            "rig": self.rig,
            "machine": self.machine,
            "loadavg_after": list(self.loadavg_after) if self.loadavg_after else None,
            "errors": self.errors,
            "interrupt": None if self.interrupt is None else _interrupt_json(self.interrupt),
            "soak": None
            if self.soak is None
            else {
                "minutes": self.soak.minutes,
                "cycles": self.soak.cycles,
                "passed": self.soak.passed,
                "findings": self.soak.findings,
                "diags": [asdict(diag) for diag in self.soak.diags],
            },
        }

    def markdown(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        commit = self.hub.get("commit")
        fw, rig, m = self.firmware, self.rig, self.machine
        loadavg = _loadavg(cast(list[float], m["loadavg"]))
        lines = [
            f"# HIL {self.name.removeprefix('hil-')}, {self.date:%Y-%m-%d %H:%M} UTC: {verdict}",
            "",
            f"Hub {self.hub['version']}{f' ({commit})' if commit else ''}, firmware "
            f"{fw['version']} (elf {fw['elf']}{', flashed for this run' if fw['flashed'] else ''})"
            f" on the Atom Echo {rig.get('device_id', '')} at {rig.get('echo_ip')}, from "
            f"{rig.get('iface')} at {rig.get('hub_ip')}. {m['cpu']}, {m['threads']} threads, "
            f"governor {m['governor'] or 'unknown'}, loadavg {loadavg} at the start"
            + (f" and {_loadavg(self.loadavg_after)} at the end." if self.loadavg_after else "."),
            "",
            "| Check | Target | Result |",
            "|---|---|---|",
        ]
        if (interrupt := self.interrupt) is not None:
            gate = interrupt.gate
            runs = ", ".join(
                f"{_ms(gate.value(i))}{' (miss)' if gate.missed(i) else ''}"
                for i in range(len(gate.runs))
            )
            lines.append(
                f"| Interrupt: injected `btn` edge → agent socket | p{gate.p} ≤ {gate.ms:g} ms, "
                f"2 of 3 runs | {runs}: {_verdict(interrupt.passed)} |"
            )
        if (soak := self.soak) is not None:
            lines.append(
                f"| Soak: {soak.minutes:g} min | heap_min flat and ≥40 KB, no resets, "
                f"stack_hwm >512 B, no reconnects | {_verdict(soak.passed)} |"
            )
        lines.append("")
        if self.errors:
            lines += ["Broke off:", "", *(f"- **{error}**" for error in self.errors), ""]
        if interrupt is not None:
            lines += _interrupt_md(interrupt)
        if soak is not None:
            lines += _soak_md(soak)
        lines += ["## Rig", "", *(f"- {key}: {value}" for key, value in rig.items()), ""]
        lines += [
            "## Machine",
            "",
            f"- CPU: {m['cpu']}, {m['threads']} threads, governor {m['governor'] or 'unknown'}",
            f"- Memory: {m['memory_gb']} GB",
            f"- OS: {m['os']}",
            f"- Python: {m['python']}",
        ]
        return "\n".join(lines) + "\n"


def _interrupt_json(interrupt: Interrupt) -> JsonObject:
    gate = interrupt.gate
    return {
        "passed": interrupt.passed,
        "findings": interrupt.findings,
        "gate": {
            "key": gate.key,
            "title": gate.title,
            "p": gate.p,
            "ms": gate.ms,
            "passed": gate.passed,
            "runs": [
                {
                    "value_ms": _finite(gate.value(i)),
                    "missed": gate.missed(i),
                    "samples_ms": [_finite(edge.ms) for edge in run],
                    "serial_bound_ms": [_finite(edge.bound_ms) for edge in run],
                }
                for i, run in enumerate(interrupt.edges)
            ],
        },
        "hub_latency": interrupt.latency,
    }


def _interrupt_md(interrupt: Interrupt) -> list[str]:
    latency = interrupt.latency
    lines = [
        "## Interrupt",
        "",
        "Each edge from `btn down|up` on the Echo's console, with its Reflex armed: each run's "
        "first press cuts a speaker stream, and every press opens the mic. From the edge on "
        "the Echo, the `t` of its notification by the hub's clock sync, to the SDK getting it. "
        "The serial bound is from the `btn` written on the serial line instead, which comes "
        "before the edge.",
        "",
        "| Run | Edges | Lost | p50 | p99 | Max | Serial bound p99 |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, run in enumerate(interrupt.edges, 1):
        samples = [edge.ms for edge in run]
        lines.append(
            f"| {i} | {len(run)} | {sum(math.isinf(ms) for ms in samples)} | "
            f"{_ms(percentile(samples, 50))} | {_ms(percentile(samples, 99))} | "
            f"{_ms(max(samples, default=math.inf))} | "
            f"{_ms(percentile([edge.bound_ms for edge in run], 99))} |"
        )
    lines += [
        "",
        f"The hub's `stats`, from each edge on the Echo to its write on the agent's socket: "
        f"{latency['count']} edges, p50 {latency['p50_ms']} ms, p99 {latency['p99_ms']} ms, "
        f"max {latency['max_ms']} ms.",
        "",
        *_findings(interrupt.findings, "They agree."),
    ]
    return lines


def _soak_md(soak: Soak) -> list[str]:
    if not soak.diags:
        return ["## Soak", "", *_findings(soak.findings, "")]
    first, last = soak.diags[0], soak.diags[-1]
    lowest = min(soak.diags, key=lambda d: d.heap_min)
    stacks = [(task, b) for d in soak.diags for task, b in d.stack_hwm.items()]
    task, tightest = min(stacks, key=lambda s: s[1], default=("none", 0))
    return [
        "## Soak",
        "",
        f"{soak.minutes:g} min, {soak.cycles} rounds of speaker, mic and presses.",
        "",
        f"- heap_min: {first.heap_min} B at the start, {last.heap_min} B at the end, lowest "
        f"{lowest.heap_min} B",
        f"- heap_free: {first.heap_free} B at the start, {last.heap_free} B at the end",
        f"- stack_hwm: the tightest {task}, {tightest} B",
        f"- reconnects: {last.reconnects - first.reconnects}; reset reason {last.reset_reason}, "
        f"uptime {last.uptime_s} s",
        "",
        *_findings(soak.findings, "Every invariant held."),
    ]


def _findings(findings: list[str], none: str) -> list[str]:
    return [*(f"- **{f}**" for f in findings), ""] if findings else [none, ""]


def _verdict(passed: bool) -> str:
    return "pass" if passed else "**FAIL**"


def _ms(ms: float) -> str:
    return "lost" if math.isinf(ms) else f"{ms:.1f} ms"


def _loadavg(loadavg: Sequence[float]) -> str:
    return "/".join(f"{load:.2f}" for load in loadavg)


def _finite(ms: float) -> float | None:
    return None if math.isinf(ms) else round(ms, 2)
