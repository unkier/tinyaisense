"""The bench's acceptance report, in Markdown and JSON, and the machine it ran on."""

import json
import math
import os
import platform
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from tinyaisense_wire import JsonObject

from ..config import EngineNames, SpeechConfig, engine_names
from .gates import Gate, percentile

ESCAPE_HATCH = (
    "The interrupt gate missed, which triggers the escape hatch: move the `stt` and `tts` roles "
    "to a worker subprocess, amending ADR 0004 (spec #18, risk 2)."
)


@dataclass(frozen=True, slots=True)
class Heard:
    """What the STT made of one clip, in one run."""

    clip: str
    lang: str
    ms: float
    transcript: str
    errors: int
    """Words it got wrong."""
    words: int
    """Words said."""


@dataclass(frozen=True, slots=True)
class Spoke:
    """How soon one sentence was heard, in one run."""

    lang: str
    text: str
    ms: float


@dataclass(frozen=True, slots=True)
class Load:
    """What one run of presses got, and the load it ran under."""

    presses: int
    lost: int
    """Presses whose `button.down` never came."""
    says: int
    utterances: int


@dataclass(slots=True)
class Report:
    name: str
    date: datetime
    hub: JsonObject
    """`version`, and `commit` if known."""
    machine: JsonObject
    """From `machine()`, at the start."""
    speech: SpeechConfig
    gates: list[Gate]
    heard: list[list[Heard]] = field(default_factory=list[list[Heard]])
    """The STT gate's, by run."""
    spoke: list[list[Spoke]] = field(default_factory=list[list[Spoke]])
    """The TTS gate's, by run."""
    load: list[Load] = field(default_factory=list[Load])
    """The interrupt gate's, by run."""
    button_latency: JsonObject | None = None
    """The hub's own `stats` of every button edge of the bench, push-to-talk's too."""
    loadavg_after: tuple[float, float, float] | None = None

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)

    @property
    def escape_hatch(self) -> bool:
        return any(gate.key == "interrupt" and not gate.passed for gate in self.gates)

    def wer(self) -> dict[str, float]:
        """The word error rate of each language, over every run."""
        errors: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
        for heard in (h for run in self.heard for h in run):
            errors[heard.lang][0] += heard.errors
            errors[heard.lang][1] += heard.words
        return {lang: wrong / max(1, said) for lang, (wrong, said) in errors.items()}

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
            "escape_hatch": self.escape_hatch,
            "hub": self.hub,
            "machine": self.machine,
            "loadavg_after": list(self.loadavg_after) if self.loadavg_after else None,
            "engines": _engines(self.speech),
            "gates": [
                {
                    "key": gate.key,
                    "title": gate.title,
                    "p": gate.p,
                    "ms": gate.ms,
                    "runs": [
                        {
                            "value_ms": _finite(gate.value(i)),
                            "missed": gate.missed(i),
                            "samples_ms": [_finite(ms) for ms in run],
                        }
                        for i, run in enumerate(gate.runs)
                    ],
                    "passed": gate.passed,
                }
                for gate in self.gates
            ],
            "stt": {
                "wer": self.wer(),
                "runs": [[_json(heard) for heard in run] for run in self.heard],
            },
            "tts": {"runs": [[_json(spoke) for spoke in run] for run in self.spoke]},
            "interrupt": {
                "runs": [_json(load) for load in self.load],
                "button_latency": self.button_latency,
            },
        }

    def markdown(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        lines = [f"# Speech bench {self.name}, {self.date:%Y-%m-%d %H:%M} UTC: {verdict}", ""]
        if self.escape_hatch:
            lines += [f"**{ESCAPE_HATCH}**", ""]
        commit = self.hub.get("commit")
        m = self.machine
        lines += [
            f"Hub {self.hub['version']}{f' ({commit})' if commit else ''} on {m['cpu']}, "
            f"{m['threads']} threads, governor {m['governor'] or 'unknown'}, "
            f"loadavg {_loadavg(m['loadavg'])} at the start"
            + (f" and {_loadavg(self.loadavg_after)} at the end." if self.loadavg_after else "."),
            "",
            "| Gate | Target | Runs | Result |",
            "|---|---|---|---|",
        ]
        for gate in self.gates:
            runs = ", ".join(
                f"{_ms(gate.value(i))}{' (miss)' if gate.missed(i) else ''}"
                for i in range(len(gate.runs))
            )
            lines.append(
                f"| {gate.title} | p{gate.p} ≤ {gate.ms:g} ms | {runs} | "
                f"{'pass' if gate.passed else '**FAIL**'} |"
            )
        lines += ["", "A gate fails only if most of its runs miss: 2 of 3.", ""]
        if self.heard:
            lines += self._stt()
        if self.spoke:
            lines += self._tts()
        if self.load:
            lines += self._interrupt()
        lines += self._machine()
        return "\n".join(lines) + "\n"

    def _stt(self) -> list[str]:
        wer = self.wer()
        lines = [
            "## STT",
            "",
            "From the release of a push-to-talk press, right after the last frame of speech, "
            "to the SDK getting `utterance.end`, over every run. WER is reported, not gated.",
            "",
            "| Lang | Samples | p50 | p95 | Max | WER |",
            "|---|---|---|---|---|---|",
        ]
        for lang, samples in _by_lang((h.lang, h.ms) for run in self.heard for h in run):
            lines.append(f"| {lang} | {_row(samples)} | {wer[lang]:.1%} |")
        return [*lines, ""]

    def _tts(self) -> list[str]:
        lines = [
            "## TTS",
            "",
            "From the SDK sending `say` to the first 10 ms frame reaching the device, over every "
            "run.",
            "",
            "| Lang | Samples | p50 | p95 | Max |",
            "|---|---|---|---|---|",
        ]
        for lang, samples in _by_lang((s.lang, s.ms) for run in self.spoke for s in run):
            lines.append(f"| {lang} | {_row(samples)} |")
        return [*lines, ""]

    def _interrupt(self) -> list[str]:
        gate = next(gate for gate in self.gates if gate.key == "interrupt")
        lines = [
            "## Interrupt",
            "",
            "From a simulated press to the SDK getting `button.down`, the hub in a process of its "
            "own, while simulated devices keep its STT and TTS busy.",
            "",
            "| Run | Presses | Lost | p50 | p99 | Max | Says | Utterances |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for i, (run, load) in enumerate(zip(gate.runs, self.load, strict=True), 1):
            lines.append(
                f"| {i} | {load.presses} | {load.lost} | {_ms(percentile(run, 50))} | "
                f"{_ms(percentile(run, 99))} | {_ms(max(run, default=math.inf))} | {load.says} | "
                f"{load.utterances} |"
            )
        if (latency := self.button_latency) is not None:
            lines += [
                "",
                f"The hub's `stats`, from the device's edge to the agent's socket, over the "
                f"whole bench: {latency['count']} edges, p50 {latency['p50_ms']} ms, "
                f"p99 {latency['p99_ms']} ms, max {latency['max_ms']} ms.",
            ]
        return [*lines, ""]

    def _machine(self) -> list[str]:
        m = self.machine
        lines = [
            "## Machine",
            "",
            f"- CPU: {m['cpu']}, {m['threads']} threads, governor {m['governor'] or 'unknown'}",
            f"- Memory: {m['memory_gb']} GB",
            f"- OS: {m['os']}",
            f"- Python: {m['python']}",
            "",
            "## Engines",
            "",
        ]
        for role, engine in _engines(self.speech).items():
            lines.append(f"- {role}: {engine}")
        return lines


def machine() -> JsonObject:
    """What the bench runs on: CPU, governor, load, memory, OS and Python."""
    return {
        "cpu": _cpu() or platform.processor() or platform.machine(),
        "threads": os.cpu_count(),
        "governor": _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "loadavg": [round(load, 2) for load in os.getloadavg()],
        "memory_gb": _memory_gb(),
        "os": platform.platform(),
        "python": platform.python_version(),
    }


def _cpu() -> str | None:
    for line in (_read("/proc/cpuinfo") or "").splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "model name":
            return value.strip()
    return None


def _memory_gb() -> float | None:
    for line in (_read("/proc/meminfo") or "").splitlines():
        if line.startswith("MemTotal:"):
            return round(int(line.split()[1]) / 1024**2, 1)
    return None


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _engines(speech: SpeechConfig) -> dict[str, str]:
    """Each role's engine, by its name and class, and its stand-in if it's a cloud engine."""

    def engine(name: str) -> str:
        config = speech.engines[name]
        params = ", ".join(f"{k}={v}" for k, v in config.params.items())
        return f"{name} ({config.cls}{f', {params}' if params else ''})"

    roles: dict[str, EngineNames] = {
        role: name for role, name in (("vad", speech.vad), ("turn", speech.turn)) if name
    }
    roles |= {f"stt.{lang}": names for lang, names in speech.stt.items()}
    roles |= {f"tts.{lang}": names for lang, names in speech.tts.items()}
    return {
        role: ", falling back to ".join(map(engine, engine_names(names)))
        for role, names in roles.items()
    }


def _by_lang(samples: Iterable[tuple[str, float]]) -> list[tuple[str, list[float]]]:
    by: defaultdict[str, list[float]] = defaultdict(list)
    for lang, ms in samples:
        by[lang].append(ms)
    return sorted(by.items())


def _row(samples: Sequence[float]) -> str:
    return (
        f"{len(samples)} | {_ms(percentile(samples, 50))} | {_ms(percentile(samples, 95))} | "
        f"{_ms(max(samples, default=math.inf))}"
    )


def _ms(ms: float) -> str:
    return "lost" if math.isinf(ms) else f"{ms:.1f} ms"


def _loadavg(loadavg: Iterable[float]) -> str:
    return "/".join(f"{load:.2f}" for load in loadavg)


def _finite(ms: float) -> float | None:
    """`ms` for JSON, rounded; None for a lost sample."""
    return None if math.isinf(ms) else round(ms, 2)


def _json(sample: Heard | Spoke | Load) -> JsonObject:
    return {k: _finite(v) if k == "ms" else v for k, v in asdict(sample).items()}
