"""The `tinyaisense-hub` command line."""

import asyncio
import os
import re
import signal
import socket
import stat
import sys
import tomllib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import tinyaisense
import uvloop
from tinyaisense_hub.cli import main, run_hub
from tinyaisense_sim.pytest_plugin import SimulatorFactory


@pytest.fixture
def xdg(tmp_path: Path, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Point the XDG dirs into the test, for this process and its children."""
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_RUNTIME_DIR": str(runtime_dir),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return env


def replace_speech(text: str, speech: str = "") -> str:
    """A config `init` wrote, with `speech` in place of its [speech] section, and so of the
    local engines, which need models."""
    return re.split(r"^\[speech\]$", text, flags=re.M)[0] + speech


def test_init_writes_a_config_with_a_random_agent_token(
    xdg: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == 0

    path = Path(xdg["XDG_CONFIG_HOME"]) / "tinyaisense" / "hub.toml"
    assert str(path) in capsys.readouterr().out
    config = tomllib.loads(path.read_text())
    assert config["ports"] == {"device": 8420, "http": 8421}
    assert config["timing"] == {
        "hello_timeout_ms": 5000,
        "ping_interval_ms": 2000,
        "dead_after_ms": 6000,
        "clock_window_ms": 20000,
        "agent_ping_interval_ms": 2000,
        "agent_dead_after_ms": 6000,
        "speaker_buffer_ms": 300000,
        "mic_tail_ms": 30,
        "agent_audio_ms": 2000,
        "segment_silence_ms": 200,
        "turn_silence_ms": 1000,
        "no_speech_ms": 8000,
        "listen_max_ms": 60000,
        "thinking_ms": 60000,
        "event_hold_ms": 30,
        "click_ms": 250,
        "double_click_ms": 300,
        "long_press_ms": 1000,
        "cloud_first_byte_ms": 1000,
        "cloud_skip_ms": 60000,
        "mcp_idle_ms": 600000,
        "mcp_max_block_ms": 50000,
        "mcp_progress_ms": 5000,
    }
    assert config["allowed_hosts"] == ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    assert config["speech"] == {
        "lang": "en",
        "say_ahead_ms": 3000,
        "speaker_latency_ms": 50,
        "vad": "silero_vad",
        "turn": "smart_turn",
        "tts": {"en": "piper", "ru": "silero_tts"},
        "stt": {"en": "parakeet", "ru": "parakeet"},
        "engines": {
            "silero_vad": {
                "class": "tinyaisense_hub.speech.local:SileroVad",
                "params": {"threads": 1},
            },
            "smart_turn": {
                "class": "tinyaisense_hub.speech.local:SmartTurn",
                "params": {"threads": 4},
            },
            "parakeet": {
                "class": "tinyaisense_hub.speech.local:ParakeetStt",
                "params": {"threads": 4},
            },
            "piper": {
                "class": "tinyaisense_hub.speech.local:PiperTts",
                "params": {"threads": 4},
            },
            "silero_tts": {
                "class": "tinyaisense_hub.speech.local:SileroTts",
                "params": {"threads": 4},
            },
        },
        "voices": [
            {"id": "lessac", "lang": "en", "name": "Lessac", "engine_voice": "en_US-lessac-medium"},
            {"id": "xenia", "lang": "ru", "name": "Xenia", "engine_voice": "xenia"},
        ],
    }
    assert len(config["agent_token"]) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_init_tokens_differ_between_configs(
    xdg: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tokens: list[str] = []
    for name in ("one", "two"):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / name))
        main(["init"])
        tokens.append(
            tomllib.loads((tmp_path / name / "tinyaisense" / "hub.toml").read_text())["agent_token"]
        )

    assert tokens[0] != tokens[1]


def test_init_leaves_an_existing_config_alone(
    xdg: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["init"])
    path = Path(xdg["XDG_CONFIG_HOME"]) / "tinyaisense" / "hub.toml"
    before = path.read_text()

    assert main(["init"]) == 1

    assert path.read_text() == before
    assert "already exists" in capsys.readouterr().err


def test_approve_takes_as_before_the_name(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["approve", "a1b2c3", "kitchen"])

    assert "approve <device> [as <name>]" in capsys.readouterr().err


def test_owner_commands_say_when_no_hub_is_running(
    xdg: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["devices"]) == 1

    assert "tinyaisense-hub serve" in capsys.readouterr().err


async def cli(env: dict[str, str], *args: str) -> str:
    """Run the CLI in a child process, so that this loop keeps serving. Returns stdout."""
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tinyaisense_hub", *args,
        env=os.environ | env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, err = await child.communicate()
    assert child.returncode == 0, err.decode()
    return out.decode()


@dataclass(frozen=True)
class Served:
    device: tuple[str, int]
    agent_url: str
    agent_token: str


SPEECH = """
[speech.tts]
en = "fake"
ru = "fake"

[speech.engines.fake]
class = "tinyaisense_hub.speech.fake:FakeTts"
params = { voices = { alto = 0, masha = 2 } }

[[speech.voices]]
id = "alto"
lang = "en"
name = "Alto"

[[speech.voices]]
id = "masha"
lang = "ru"
name = "Маша"
"""
"""The fake TTS and two Voices, in place of what `init` wrote in [speech]."""


@pytest.fixture
async def served(xdg: dict[str, str], request: pytest.FixtureRequest) -> AsyncIterator[Served]:
    """`tinyaisense-hub serve` in a child process, on free ports. Parametrize it indirectly
    with more config to append."""
    main(["init"])
    with socket.socket() as device_probe, socket.socket() as http_probe:
        device_probe.bind(("127.0.0.1", 0))
        http_probe.bind(("127.0.0.1", 0))
        port, http = device_probe.getsockname()[1], http_probe.getsockname()[1]
    config = Path(xdg["XDG_CONFIG_HOME"]) / "tinyaisense" / "hub.toml"
    text = config.read_text().replace("device = 8420", f"device = {port}")
    text = text.replace("http = 8421", f"http = {http}")
    config.write_text(replace_speech(text, getattr(request, "param", "")))
    hub = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tinyaisense_hub", "serve", "--no-mdns", env=os.environ | xdg
    )
    owner_socket = Path(xdg["XDG_RUNTIME_DIR"]) / "tinyaisense" / "owner.sock"
    while not owner_socket.exists():
        assert hub.returncode is None
        await asyncio.sleep(0.02)
    token = tomllib.loads(text)["agent_token"]
    yield Served(("127.0.0.1", port), f"ws://127.0.0.1:{http}/agent", token)
    if hub.returncode is None:
        hub.send_signal(signal.SIGTERM)
    assert await hub.wait() == 0


async def test_serve_pairs_a_device_the_owner_approves_from_the_cli(
    served: Served, xdg: dict[str, str], simulator: SimulatorFactory
) -> None:
    device = await simulator(served.device).add("m5stack-atom-echo", "kitchen")
    assert device.device_id in await cli(xdg, "devices")

    await cli(xdg, "approve", device.device_id[:6], "as", "kitchen")

    await device.wait_for(lambda: device.name == "kitchen")
    [header, row] = (await cli(xdg, "devices")).splitlines()
    assert header.split() == ["DEVICE", "ID", "NAME", "STATE", "LAST", "SEEN", "BOARD", "FIRMWARE"]
    assert row.split()[1:4] == ["kitchen", "online", "now"]

    await device.close()
    while (row := (await cli(xdg, "devices")).splitlines()[1]).split()[2] != "offline":
        pass
    assert re.fullmatch(r"\d{4}-\d\d-\d\d", row.split()[3])  # then the local time


async def test_the_owner_identifies_diagnoses_and_sets_the_volume_from_the_cli(
    served: Served, xdg: dict[str, str], simulator: SimulatorFactory
) -> None:
    device = await simulator(served.device).add("m5stack-atom-echo", "kitchen")
    await cli(xdg, "approve", device.device_id, "as", "kitchen")
    await device.wait_for(lambda: device.state == "online")

    identified = await cli(xdg, "identify", "kitchen", "2")
    diag = dict(line.split(maxsplit=1) for line in (await cli(xdg, "diag", "kitchen")).splitlines())
    volume = await cli(xdg, "volume", "kitchen", "35")

    assert device.status == "identify"
    assert "kitchen" in identified
    assert diag.keys() >= {"uptime_s", "reset_reason", "heap_free", "heap_min", "rssi"}
    assert (diag["reset_reason"], diag["rssi"], diag["reconnects"]) == ("poweron", "-61 dBm", "0")
    assert "ctrl=" in diag["stack_hwm"]
    assert device.speaker.level == 35
    assert volume.strip() == "kitchen: volume 35"


async def test_the_owner_sees_the_button_latency_histogram_from_the_cli(
    served: Served, xdg: dict[str, str], simulator: SimulatorFactory
) -> None:
    none_yet = await cli(xdg, "stats")
    device = await simulator(served.device).add("m5stack-atom-echo", "kitchen")
    await cli(xdg, "approve", device.device_id, "as", "kitchen")
    await device.wait_for(lambda: device.state == "online")
    async with tinyaisense.connect(served.agent_url, served.agent_token) as agent:
        notifications = agent.notifications()
        for _ in range(3):
            await device.button.click(ms=20)
            await asyncio.sleep(0.02)
        edges = 0
        async with asyncio.timeout(2):
            async for notification in notifications:
                edges += notification.method in ("button.down", "button.up")
                if edges == 6:
                    break
            while "6 edges" not in (stats := await cli(xdg, "stats")):
                pass

    assert none_yet.strip() == "button to agent socket: no edges yet"
    [summary, *buckets] = stats.splitlines()
    assert re.fullmatch(
        r"button to agent socket: 6 edges, p50 [\d.]+ ms, p99 [\d.]+ ms, max [\d.]+ ms", summary
    )
    assert sum(int(line.split()[-1]) for line in buckets) == 6
    assert all(re.fullmatch(r" *(<=\d+|>\d+) ms  #* *\d+", line) for line in buckets)


async def test_serve_serves_the_agent_api(served: Served) -> None:
    async with tinyaisense.connect(served.agent_url, served.agent_token) as agent:
        assert await agent.devices() == []


@pytest.mark.parametrize("served", [SPEECH], indirect=True)
async def test_serve_speaks_in_the_voices_of_its_config(served: Served) -> None:
    async with tinyaisense.connect(served.agent_url, served.agent_token) as agent:
        assert await agent.voices() == [
            {"id": "alto", "lang": "en", "name": "Alto"},
            {"id": "masha", "lang": "ru", "name": "Маша"},
        ]


@pytest.mark.parametrize(
    ("speech", "error"),
    [
        (SPEECH.replace('ru = "fake"', 'ru = "piper"'), "speech.tts.ru: no engine 'piper'"),
        (SPEECH.replace('lang = "ru"', 'lang = "de"'), "Voice 'masha' speaks de, which no"),
        (SPEECH.replace('ru = "fake"', ""), "Voice 'masha' speaks ru, which no"),
        (SPEECH.replace('ru = "fake"', 'ru = "fake"\nde = "fake"'), "speech.tts.de: no Voice"),
        (SPEECH + 'de = "fake"', "unknown keys: speech.voices[1].de"),
        (SPEECH.replace('id = "masha"', 'id = "alto"'), "two Voices have the id 'alto'"),
        (SPEECH.replace("fake:FakeTts", "fake:Nope"), "speech engine 'fake': can't import"),
        (SPEECH.replace("fake:FakeTts", "fake:decode"), "fake:decode isn't a Tts"),
        (
            SPEECH.replace("params = {", 'params = { languages = ["en"],'),
            "speech engine 'fake' doesn't speak ru",
        ),
        (
            SPEECH + 'engine_voice = "natasha"',
            "Voice 'masha': speech engine 'fake' has no voice 'natasha'",
        ),
        (SPEECH.replace("name = ", "title = ", 1), "speech.voices[0].name must be"),
    ],
)
def test_serve_refuses_a_speech_config_that_doesnt_hold_together(
    speech: str, error: str, xdg: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["init"])
    config = Path(xdg["XDG_CONFIG_HOME"]) / "tinyaisense" / "hub.toml"
    config.write_text(replace_speech(config.read_text(), speech))

    assert main(["serve"]) == 1

    assert error in capsys.readouterr().err


def test_serve_refuses_a_default_language_no_voice_speaks(
    xdg: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["init"])
    config = Path(xdg["XDG_CONFIG_HOME"]) / "tinyaisense" / "hub.toml"
    config.write_text(replace_speech(config.read_text(), '[speech]\nlang = "de"\n' + SPEECH))

    assert main(["serve"]) == 1

    assert "speech.lang: no Voice speaks de" in capsys.readouterr().err


LISTEN = """
[speech]
vad = "vad"
turn = "turn"

[speech.stt]
en = "stt"
ru = "stt"

[speech.engines.vad]
class = "tinyaisense_hub.speech.fake:FakeVad"

[speech.engines.turn]
class = "tinyaisense_hub.speech.fake:FakeTurn"

[speech.engines.stt]
class = "tinyaisense_hub.speech.fake:FakeStt"
"""
"""The fake listening engines, in place of what `init` wrote in [speech]."""


@pytest.mark.parametrize(
    ("speech", "error"),
    [
        (LISTEN.replace('vad = "vad"', ""), "speech.stt needs a speech.vad engine"),
        (LISTEN.replace('vad = "vad"', 'vad = "silero"'), "speech.vad: no engine 'silero'"),
        (LISTEN.replace('turn = "turn"', 'turn = "smart"'), "speech.turn: no engine 'smart'"),
        (LISTEN.replace('ru = "stt"', 'ru = "vosk"'), "speech.stt.ru: no engine 'vosk'"),
        (LISTEN.replace("fake:FakeVad", "fake:FakeStt"), "fake:FakeStt isn't a Vad"),
        (
            LISTEN.replace('FakeStt"', 'FakeStt"\nparams = { languages = ["en"] }'),
            "speech engine 'stt' doesn't hear ru",
        ),
        (
            LISTEN.replace('FakeStt"', 'FakeStt"\nparams = { rate = 24000 }'),
            "speech engine 'stt' hears at 24000 Hz: the hub listens at 16000 Hz only",
        ),
    ],
)
def test_serve_refuses_listening_engines_that_dont_hold_together(
    speech: str, error: str, xdg: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["init"])
    config = Path(xdg["XDG_CONFIG_HOME"]) / "tinyaisense" / "hub.toml"
    config.write_text(replace_speech(config.read_text(), speech))

    assert main(["serve"]) == 1

    assert error in capsys.readouterr().err


def test_serve_refuses_to_start_without_the_models_of_its_engines(
    xdg: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("sherpa_onnx", reason="the local engines need the [local] extra")
    main(["init"])

    assert main(["serve"]) == 1

    err = capsys.readouterr().err
    models = Path(xdg["XDG_DATA_HOME"]) / "tinyaisense" / "models"
    assert f"speech engine 'piper': no model at {models / 'piper-en_US-lessac-medium'}" in err
    assert "run `tinyaisense-hub models fetch`" in err


def test_serve_refuses_a_turn_silence_within_the_segment_silence(
    xdg: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["init"])
    config = Path(xdg["XDG_CONFIG_HOME"]) / "tinyaisense" / "hub.toml"
    config.write_text(config.read_text().replace("turn_silence_ms = 1000", "turn_silence_ms = 200"))

    assert main(["serve"]) == 1

    assert "timing.turn_silence_ms must be more than" in capsys.readouterr().err


@pytest.mark.parametrize("hosts", ['"hub.local:*"', '["hub.local:*", ""]', "[8421]"])
def test_serve_refuses_allowed_hosts_that_arent_host_names(
    hosts: str, xdg: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["init"])
    config = Path(xdg["XDG_CONFIG_HOME"]) / "tinyaisense" / "hub.toml"
    text = re.sub(
        r"^allowed_hosts = .*$", f"allowed_hosts = {hosts}", config.read_text(), flags=re.M
    )
    config.write_text(replace_speech(text))

    assert main(["serve"]) == 1

    assert "allowed_hosts must be an array of non-empty strings" in capsys.readouterr().err


def test_serve_says_when_its_port_is_taken(
    xdg: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["init"])
    config = Path(xdg["XDG_CONFIG_HOME"]) / "tinyaisense" / "hub.toml"
    with socket.socket() as taken:
        taken.bind(("0.0.0.0", 0))
        taken.listen()
        port = taken.getsockname()[1]
        text = config.read_text().replace("device = 8420", f"device = {port}")
        config.write_text(replace_speech(text))

        assert main(["serve"]) == 1

    assert "address already in use" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("key", ["dead_after_ms", "agent_dead_after_ms"])
def test_serve_refuses_a_dead_timeout_within_the_ping_interval(
    key: str, xdg: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["init"])
    config = Path(xdg["XDG_CONFIG_HOME"]) / "tinyaisense" / "hub.toml"
    config.write_text(re.sub(rf"^{key} = 6000$", f"{key} = 2000", config.read_text(), flags=re.M))

    assert main(["serve"]) == 1

    assert f"timing.{key} must be more than" in capsys.readouterr().err


def test_the_hub_runs_on_uvloop_with_a_1_ms_switch_interval() -> None:
    async def probe() -> tuple[bool, float]:
        return isinstance(asyncio.get_running_loop(), uvloop.Loop), sys.getswitchinterval()

    before = sys.getswitchinterval()

    assert run_hub(probe()) == (True, 0.001)
    assert sys.getswitchinterval() == before
