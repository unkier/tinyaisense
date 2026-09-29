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
        "XDG_RUNTIME_DIR": str(runtime_dir),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return env


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


@pytest.fixture
async def served(xdg: dict[str, str]) -> AsyncIterator[Served]:
    """`tinyaisense-hub serve` in a child process, on free ports."""
    main(["init"])
    with socket.socket() as device_probe, socket.socket() as http_probe:
        device_probe.bind(("127.0.0.1", 0))
        http_probe.bind(("127.0.0.1", 0))
        port, http = device_probe.getsockname()[1], http_probe.getsockname()[1]
    config = Path(xdg["XDG_CONFIG_HOME"]) / "tinyaisense" / "hub.toml"
    text = config.read_text().replace("device = 8420", f"device = {port}")
    config.write_text(text.replace("http = 8421", f"http = {http}"))
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


async def test_serve_serves_the_agent_api(served: Served) -> None:
    async with tinyaisense.connect(served.agent_url, served.agent_token) as agent:
        assert await agent.devices() == []


def test_serve_says_when_its_port_is_taken(
    xdg: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["init"])
    config = Path(xdg["XDG_CONFIG_HOME"]) / "tinyaisense" / "hub.toml"
    with socket.socket() as taken:
        taken.bind(("0.0.0.0", 0))
        taken.listen()
        port = taken.getsockname()[1]
        config.write_text(config.read_text().replace("device = 8420", f"device = {port}"))

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
