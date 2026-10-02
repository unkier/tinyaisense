"""The HIL tier's acceptance report, in Markdown and JSON, over whatever of it ran: the Interrupt
gate, the soak and the acoustic rig's gates, with the firmware and hub versions, the rig and the
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

from .acoustic import CLICK_DB, CLICK_MS, ONSET_DB
from .checks import (
    LAG_BUDGET_MS,
    CutSample,
    Diag,
    Edge,
    calibration_findings,
    click_findings,
    interrupt_findings,
    pod_lag,
    soak_findings,
)


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
class Calibration:
    """The POD's lag at each Reflex cut of a tone it was calibrated by, `inf` at one it didn't
    hear: how much later than the Echo went silent it heard the tone end, by its ADC times."""

    lags_ms: list[float]

    @property
    def lag_ms(self) -> float:
        return pod_lag(self.lags_ms)

    @property
    def findings(self) -> list[str]:
        return calibration_findings(self.lags_ms)

    @property
    def passed(self) -> bool:
        return not self.findings


@dataclass(slots=True)
class MouthToHub:
    gate: Gate
    """From each chirp's onset at the POD to the agent getting the mic frame it starts in."""
    pod_lag_ms: float
    """What was taken off the POD's times."""

    @property
    def passed(self) -> bool:
        return self.gate.passed


@dataclass(slots=True)
class HubToSpeaker:
    gate: Gate
    """From each `say` in `voice` to its onset at the POD."""
    voice: str
    reported: dict[str, list[float]]
    """Samples reported, not gated, by what: e.g. a `say` in another Voice, or raw PCM from its
    `stream.start`."""
    start_clicks_db: list[float]
    """How far each raw PCM stream's start clicked, from its `stream.start` to its onset, in dB
    over the noise floor: reported, not gated."""

    @property
    def passed(self) -> bool:
        return self.gate.passed


@dataclass(slots=True)
class SpeakerCut:
    gate: Gate
    """From each press to silence, by the firmware's own timing."""
    cuts: list[list[CutSample]]
    """Each run's."""
    control_db: list[float]
    """How far each cut by stopping the I2S channel clicked: the click check's positive control."""

    @property
    def findings(self) -> list[str]:
        tones = [[cut.click_db for cut in run if cut.playing == "tone"] for run in self.cuts]
        return click_findings(tones, self.control_db)

    @property
    def passed(self) -> bool:
        return self.gate.passed and not self.findings


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
    calibration: Calibration | None = None
    mouth_to_hub: MouthToHub | None = None
    hub_to_speaker: HubToSpeaker | None = None
    speaker_cut: SpeakerCut | None = None
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
        parts = (
            self.interrupt,
            self.soak,
            self.calibration,
            self.mouth_to_hub,
            self.hub_to_speaker,
            self.speaker_cut,
        )
        return not self.errors and all(part is None or part.passed for part in parts)

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
            **self._acoustic_json(),
        }

    def _acoustic_json(self) -> JsonObject:
        calibration, mouth, say, cut = (
            self.calibration,
            self.mouth_to_hub,
            self.hub_to_speaker,
            self.speaker_cut,
        )
        return {
            "calibration": None
            if calibration is None
            else {
                "passed": calibration.passed,
                "findings": calibration.findings,
                "lag_ms": _finite(calibration.lag_ms),
                "lags_ms": [_finite(lag) for lag in calibration.lags_ms],
            },
            "mouth_to_hub": None
            if mouth is None
            else {
                "passed": mouth.passed,
                "pod_lag_ms": mouth.pod_lag_ms,
                "gate": _gate(mouth.gate),
            },
            "hub_to_speaker": None
            if say is None
            else {
                "passed": say.passed,
                "voice": say.voice,
                "gate": _gate(say.gate),
                "reported": {
                    what: [_finite(ms) for ms in samples] for what, samples in say.reported.items()
                },
                "start_clicks_db": [_finite(db) for db in say.start_clicks_db],
            },
            "speaker_cut": None
            if cut is None
            else {
                "passed": cut.passed,
                "findings": cut.findings,
                "gate": _gate(cut.gate),
                "runs": [{"cuts": [_cut_json(sample) for sample in run]} for run in cut.cuts],
                "control_db": [_finite(db) for db in cut.control_db],
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
        lines += self._acoustic_rows()
        lines.append("")
        if self.errors:
            lines += ["Broke off:", "", *(f"- **{error}**" for error in self.errors), ""]
        if interrupt is not None:
            lines += _interrupt_md(interrupt)
        if soak is not None:
            lines += _soak_md(soak)
        lines += self._acoustic_md()
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

    def _acoustic_rows(self) -> list[str]:
        rows: list[str] = []
        if (calibration := self.calibration) is not None:
            rows.append(
                f"| POD calibration: its lag, against the Echo's own cut timing | within "
                f"±{LAG_BUDGET_MS:g} ms of its median, p95 | {_ms(calibration.lag_ms)}: "
                f"{_verdict(calibration.passed)} |"
            )
        if (mouth := self.mouth_to_hub) is not None:
            rows.append(_gate_row(mouth.gate, mouth.passed))
        if (say := self.hub_to_speaker) is not None:
            rows.append(_gate_row(say.gate, say.passed))
        if (cut := self.speaker_cut) is not None:
            rows.append(_gate_row(cut.gate, cut.gate.passed))
            loudest = [
                max((c.click_db for c in run if c.playing == "tone"), default=math.inf)
                for run in cut.cuts
            ]
            rows.append(
                f"| Cut click: energy over 2 kHz after the ramp, tone cuts | ≤{CLICK_DB:g} dB "
                f"over the noise floor, 2 of 3 runs; the positive control heard | loudest "
                f"{', '.join(_db(db) for db in loudest)}: {_verdict(not cut.findings)} |"
            )
        return rows

    def _acoustic_md(self) -> list[str]:
        lines: list[str] = []
        if (calibration := self.calibration) is not None:
            lines += _calibration_md(calibration)
        if (mouth := self.mouth_to_hub) is not None:
            lines += [
                "## mouth→hub",
                "",
                "The rig's PC speaker plays chirps into the Echo's mic, on a raw mic stream. From "
                "each chirp's onset at the POD, by a matched filter, with the POD's lag of "
                f"{mouth.pod_lag_ms:.1f} ms taken off, to the agent getting the mic frame the "
                "chirp starts in, found by a matched filter too. The agent gets it a little "
                "after the hub does.",
                "",
                *_runs_md(mouth.gate),
            ]
        if (say := self.hub_to_speaker) is not None:
            lines += _hub_to_speaker_md(say)
        if (cut := self.speaker_cut) is not None:
            lines += _speaker_cut_md(cut)
        return lines


def _gate_row(gate: Gate, passed: bool) -> str:
    runs = ", ".join(
        f"{_ms(gate.value(i))}{' (miss)' if gate.missed(i) else ''}" for i in range(len(gate.runs))
    )
    target = f"p{gate.p} ≤ {gate.ms:g} ms, 2 of 3 runs"
    return f"| {gate.title} | {target} | {runs}: {_verdict(passed)} |"


def _runs_md(gate: Gate) -> list[str]:
    lines = ["| Run | Samples | Lost | p50 | p99 | Max |", "|---|---|---|---|---|---|"]
    for i, run in enumerate(gate.runs, 1):
        lines.append(f"| {i} | {len(run)} | {sum(math.isinf(ms) for ms in run)} | {_spread(run)} |")
    return [*lines, ""]


def _spread(samples: Sequence[float]) -> str:
    """p50, p99 and max, as table cells."""
    return (
        f"{_ms(percentile(samples, 50))} | {_ms(percentile(samples, 99))} | "
        f"{_ms(max(samples, default=math.inf))}"
    )


def _calibration_md(calibration: Calibration) -> list[str]:
    heard = [lag for lag in calibration.lags_ms if math.isfinite(lag)]
    return [
        "## POD calibration",
        "",
        "The POD hears later than PortAudio's ADC times say: its own processing isn't in "
        "them. Its lag is calibrated by Reflex cuts of a 1 kHz tone: from when the Echo went "
        "silent, by the press's `t` on the hub's clock plus the firmware's own timing of the "
        "cut, to when the POD heard the tone end. Its median is taken off the POD's times "
        "after. It includes what the Echo's I2S FIFO holds past the firmware's timing, a few "
        "ms: hub→speaker comes out that much short, mouth→hub that much long.",
        "",
        f"- lag: {_ms(calibration.lag_ms)}, over {len(heard)} of "
        f"{len(calibration.lags_ms)} cuts heard; from {_ms(min(heard, default=math.inf))} "
        f"to {_ms(max(heard, default=math.inf))}",
        "",
        *_findings(calibration.findings, f"It holds within ±{LAG_BUDGET_MS:g} ms."),
    ]


def _hub_to_speaker_md(say: HubToSpeaker) -> list[str]:
    clicks = say.start_clicks_db
    lines = [
        "## hub→speaker",
        "",
        f"From each `say` in {say.voice}, real TTS, as the agent sent it, to its acoustic "
        "onset at the POD, with the POD's lag taken off: where its level rises "
        f"{ONSET_DB:g} dB over the noise floor and mostly stays there.",
        "",
        *_runs_md(say.gate),
    ]
    if say.reported:
        lines += [
            "Reported, not gated:",
            "",
            "| What | Samples | Lost | p50 | p99 | Max |",
            "|---|---|---|---|---|---|",
            *(
                f"| {what} | {len(samples)} | {sum(math.isinf(ms) for ms in samples)} | "
                f"{_spread(samples)} |"
                for what, samples in say.reported.items()
            ),
            "",
        ]
    if clicks:
        lines += [
            f"Start click, reported: {sum(db > CLICK_DB for db in clicks)} of {len(clicks)} raw "
            f"PCM streams' starts had energy over 2 kHz more than {CLICK_DB:g} dB over the "
            f"noise floor before their onset; the loudest {_db(max(clicks))}.",
            "",
        ]
    return lines


def _speaker_cut_md(cut: SpeakerCut) -> list[str]:
    lines = [
        "## Speaker cut",
        "",
        "Each cut by an injected `btn down`, with the Reflex armed, at a random point of a 1 kHz "
        "tone or of a `say`: from the press to silence, by the firmware's own timing, which "
        "it logs. Heard: from the press to the POD hearing the tone end, with its lag taken "
        "off: the firmware's timing again, if the lag calibrated before held. Clicks: energy "
        f"over 2 kHz in the {CLICK_MS:g} ms after the ramp, against the noise floor; for a "
        "tone, with the tone "
        "notched out, against the floor while it played; for speech, against the floor after "
        "it.",
        "",
        "| Run | Cuts | Lost | p50 | p99 | Max | Heard p50 | Tone clicks | Speech clicks |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, run in enumerate(cut.cuts, 1):
        silent = [c.silent_ms for c in run]
        heard = [c.heard_ms for c in run if c.heard_ms is not None]
        lines.append(
            f"| {i} | {len(run)} | {sum(math.isinf(ms) for ms in silent)} | {_spread(silent)} | "
            f"{_ms(percentile(heard, 50))} | {_clicks(run, 'tone')} | {_clicks(run, 'speech')} |"
        )
    control = cut.control_db
    clicked = sum(db > CLICK_DB for db in control)
    lines += [
        "",
        f"Positive control, cut by stopping the I2S channel: {clicked} of {len(control)} "
        f"clicked, at {', '.join(_db(db) for db in control)}.",
        "",
        *_findings(cut.findings, "No cut clicked, and the check heard the control's clicks."),
    ]
    return lines


def _clicks(run: Sequence[CutSample], playing: str) -> str:
    """How many of `run`'s cuts while `playing` clicked, of how many, and the loudest."""
    dbs = [c.click_db for c in run if c.playing == playing]
    if not dbs:
        return "none"
    return f"{sum(db > CLICK_DB for db in dbs)} of {len(dbs)}, loudest {_db(max(dbs))}"


def _cut_json(cut: CutSample) -> JsonObject:
    return {
        "playing": cut.playing,
        "silent_ms": _finite(cut.silent_ms),
        "heard_ms": None if cut.heard_ms is None else _finite(cut.heard_ms),
        "click_db": _finite(cut.click_db),
    }


def _gate(gate: Gate) -> JsonObject:
    return {
        "key": gate.key,
        "title": gate.title,
        "p": gate.p,
        "ms": gate.ms,
        "passed": gate.passed,
        "runs": [
            {
                "value_ms": _finite(gate.value(i)),
                "missed": gate.missed(i),
                "samples_ms": [_finite(ms) for ms in run],
            }
            for i, run in enumerate(gate.runs)
        ],
    }


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


def _db(db: float) -> str:
    return "unheard" if math.isinf(db) else f"{db:.1f} dB"


def _loadavg(loadavg: Sequence[float]) -> str:
    return "/".join(f"{load:.2f}" for load in loadavg)


def _finite(ms: float) -> float | None:
    return None if math.isinf(ms) else round(ms, 2)
