"""`bench speech`, on a hub with the fake speech plugin, in a process of its own: what it
measures, and the report it writes. No timing is asserted: the model tier gates it, on the real
engines."""

import hashlib
import io
import json
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import soundfile
from tinyaisense_hub import Config, EngineConfig, SpeechConfig, VoiceConfig
from tinyaisense_hub.bench import corpus
from tinyaisense_hub.bench.corpus import BenchError, Clip, Sentence
from tinyaisense_hub.bench.gates import Gate
from tinyaisense_hub.bench.report import ESCAPE_HATCH, Report
from tinyaisense_hub.bench.run import Spoken, bench
from tinyaisense_hub.cli import main
from tinyaisense_hub.config import APP, write_config
from tinyaisense_hub.speech import fake

from .test_bench_corpus import archive

FAKE = "tinyaisense_hub.speech.fake"
SPEECH = SpeechConfig(
    engines={
        "tts": EngineConfig(f"{FAKE}:FakeTts"),
        "stt": EngineConfig(f"{FAKE}:FakeStt"),
        "vad": EngineConfig(f"{FAKE}:FakeVad"),
        "turn": EngineConfig(f"{FAKE}:FakeTurn"),
    },
    tts={"en": "tts", "ru": "tts"},
    stt={"en": "stt", "ru": "stt"},
    vad="vad",
    turn="turn",
    voices=(VoiceConfig("anna", "en", "Anna"), VoiceConfig("olga", "ru", "Olga")),
)
CONFIG = Config(agent_token="bench-token", speech=SPEECH)


def spoken(name: str, lang: str, text: str) -> Spoken:
    """`text`, as the fake TTS speaks it and the fake STT hears it, between pauses. The first
    makes holding the button through it no click. The release ends the Utterance at what the
    VAD has rated by then, so the last is what that leaves out. `text` must be long enough to be
    speech: 10 ms a character."""
    return Spoken(name, lang, text, bytes(2 * 16 * 300) + fake.encode(text) + bytes(2 * 16 * 100))


SPOKEN = [
    spoken("one", "en", "hello there, how are you?"),
    spoken("two", "ru", "привет всем, как ваши дела?"),
]
SENTENCES = [Sentence("en", "Hi."), Sentence("ru", "Да.")]


@pytest.mark.timeout(120)
async def test_the_bench_times_each_gate_on_the_hub_and_hears_what_was_said() -> None:
    report = await bench(CONFIG, SPOKEN, SENTENCES, name="test", reps=2, load=True, presses=3)

    assert [(gate.key, [len(run) for run in gate.runs]) for gate in report.gates] == [
        ("stt", [2, 2]),
        ("tts", [2, 2]),
        ("interrupt", [3, 3]),
    ]
    assert [[(h.clip, h.transcript) for h in run] for run in report.heard] == [
        [("one", "hello there, how are you?"), ("two", "привет всем, как ваши дела?")]
    ] * 2
    assert report.wer() == {"en": 0.0, "ru": 0.0}
    assert [load.presses for load in report.load] == [3, 3]
    assert all(load.lost == 0 for load in report.load)
    assert report.button_latency is not None
    assert report.button_latency["count"] == 2 * (2 * 2 + 2 * 3)  # every press and release


@pytest.mark.timeout(120)
async def test_without_load_the_bench_leaves_the_interrupt_gate_out() -> None:
    report = await bench(CONFIG, SPOKEN[:1], SENTENCES[:1], name="test", reps=1)

    assert [gate.key for gate in report.gates] == ["stt", "tts"]
    assert report.load == []


def test_the_report_is_written_as_markdown_and_json_named_by_its_time(tmp_path: Path) -> None:
    report = Report(
        "abc1234",
        datetime(2026, 9, 30, 12, 0),
        {"version": "0.1.0", "commit": "abc1234"},
        machine={
            "cpu": "Ryzen",
            "threads": 16,
            "governor": "powersave",
            "loadavg": [0.5, 0.4, 0.3],
            "memory_gb": 62.0,
            "os": "Linux",
            "python": "3.14.0",
        },
        speech=SPEECH,
        gates=[Gate("stt", "STT", 95, 150, [[100.0, 120.0]])],
    )

    written = report.write(tmp_path / "acceptance")

    assert [p.name for p in written] == [
        "2026-09-30-1200-abc1234.md",
        "2026-09-30-1200-abc1234.json",
    ]
    doc = json.loads(written[1].read_text())
    assert doc["passed"] is True
    assert doc["gates"][0]["runs"] == [
        {"value_ms": 120.0, "missed": False, "samples_ms": [100.0, 120.0]}
    ]
    assert "| STT | p95 ≤ 150 ms | 120.0 ms | pass |" in written[0].read_text()


def test_a_missed_interrupt_gate_calls_for_the_escape_hatch() -> None:
    lost = float("inf")
    report = Report(
        "abc1234",
        datetime(2026, 9, 30, 12, 0),
        {"version": "0.1.0"},
        machine={
            "cpu": "Ryzen",
            "threads": 16,
            "governor": None,
            "loadavg": [0.5, 0.4, 0.3],
            "memory_gb": 62.0,
            "os": "Linux",
            "python": "3.14.0",
        },
        speech=SPEECH,
        gates=[Gate("interrupt", "Interrupt", 99, 20, [[5.0, lost], [30.0], [4.0]])],
    )

    assert not report.passed
    assert report.escape_hatch
    assert ESCAPE_HATCH in report.markdown()
    assert report.to_json()["escape_hatch"] is True
    assert report.to_json()["gates"][0]["runs"][0]["samples_ms"] == [5.0, None]


# `bench speech`, from the command line.


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner's config, of the fake speech plugin, and a corpus of `SPOKEN`, pinned in an
    archive of the test's own."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    write_config(tmp_path / "config" / APP / "hub.toml", CONFIG)
    files: dict[str, bytes] = {}
    for clip in SPOKEN:
        flac = io.BytesIO()
        soundfile.write(flac, np.frombuffer(clip.pcm, "<i2"), 16000, "PCM_16", format="FLAC")
        files[f"clips/{clip.name}.flac"] = flac.getvalue()
    source = archive(tmp_path, files)
    clips = tuple(
        Clip(
            source,
            f"clips/{clip.name}.flac",
            hashlib.sha256(files[f"clips/{clip.name}.flac"]).hexdigest(),
            clip.lang,
            clip.text,
            (300, len(clip.pcm) // 32),
        )
        for clip in SPOKEN
    )
    monkeypatch.setattr(corpus, "CORPUS", clips)
    monkeypatch.setattr(corpus, "SENTENCES", tuple(SENTENCES))


@pytest.mark.timeout(120)
@pytest.mark.usefixtures("owner")
def test_bench_speech_writes_its_report_and_passes_if_every_gate_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "acceptance"

    args = ["--reps", "1", "--out", str(out), "--name", "t", "--max-loadavg", "inf"]

    assert main(["bench", "speech", *args]) == 0

    [js, md] = sorted(out.iterdir())
    assert (md.name[15:], js.name[15:], md.stem) == ("-t.md", "-t.json", js.stem)
    doc = json.loads(js.read_text())
    assert doc["passed"] is True
    assert [h["transcript"] for h in doc["stt"]["runs"][0]] == [c.text for c in SPOKEN]
    assert f"passed: wrote {md}" in capsys.readouterr().out


@pytest.mark.usefixtures("owner")
def test_bench_speech_skips_a_busy_machine_before_it_loads_it_any_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fetch(*_: object, **__: object) -> None:
        raise AssertionError("fetched the corpus")

    monkeypatch.setattr(os, "getloadavg", lambda: (2.5, 1.0, 0.5))
    monkeypatch.setattr(corpus, "fetch", fetch)
    out = tmp_path / "acceptance"

    assert main(["bench", "speech", "--out", str(out)]) == 0

    assert not out.exists()
    assert "skipped: the load average is 2.50, over 2" in capsys.readouterr().out


@pytest.mark.parametrize("arg", ["--reps", "--presses"])
def test_bench_speech_takes_at_least_one_run_and_press(
    arg: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(["bench", "speech", arg, "0"])

    assert "0 is not at least 1" in capsys.readouterr().err


@pytest.mark.timeout(120)
async def test_a_hub_that_fails_fails_the_bench_with_the_end_of_its_log() -> None:
    broken = replace(
        CONFIG,
        speech=replace(SPEECH, engines=SPEECH.engines | {"stt": EngineConfig("nowhere:Stt")}),
    )

    with pytest.raises(BenchError, match=r"(?s)the hub exited.*exit status 1.*nowhere"):
        await bench(broken, SPOKEN, SENTENCES, reps=1)
