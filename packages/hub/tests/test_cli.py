"""The `tinyaisense-hub` command line."""

import asyncio
import os
import signal
import socket
import stat
import sys
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from tinyaisense_hub.cli import main
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
    assert config["timing"] == {"hello_timeout_ms": 5000}
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


@pytest.fixture
async def served(xdg: dict[str, str]) -> AsyncIterator[tuple[str, int]]:
    """`tinyaisense-hub serve` in a child process. Yields its device address."""
    main(["init"])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = Path(xdg["XDG_CONFIG_HOME"]) / "tinyaisense" / "hub.toml"
    config.write_text(config.read_text().replace("device = 8420", f"device = {port}"))
    hub = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tinyaisense_hub", "serve", env=os.environ | xdg
    )
    owner_socket = Path(xdg["XDG_RUNTIME_DIR"]) / "tinyaisense" / "owner.sock"
    while not owner_socket.exists():
        assert hub.returncode is None
        await asyncio.sleep(0.02)
    yield "127.0.0.1", port
    if hub.returncode is None:
        hub.send_signal(signal.SIGTERM)
    assert await hub.wait() == 0


async def test_serve_pairs_a_device_the_owner_approves_from_the_cli(
    served: tuple[str, int], xdg: dict[str, str], simulator: SimulatorFactory
) -> None:
    device = await simulator(served).add("m5stack-atom-echo", "kitchen")
    assert device.device_id in await cli(xdg, "devices")

    await cli(xdg, "approve", device.device_id[:6], "as", "kitchen")

    await device.wait_for(lambda: device.name == "kitchen")
    listing = await cli(xdg, "devices")
    assert "kitchen" in listing
    assert "pending" not in listing


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
