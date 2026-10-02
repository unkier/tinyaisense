"""The hub under test, run as `serve --auto-approve --no-mdns` runs it, in a process of its own:
the model tier's bench runs it on loopback, with simulated devices, and the HIL tier on the
LAN, with the Echo."""

import asyncio
import contextlib
import dataclasses
import os
import random
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path
from types import TracebackType
from typing import Self, cast

import tinyaisense
from tinyaisense_wire import JsonObject

from ..config import APP, Config, write_config
from ..owner import SOCKET_NAME, OwnerClient
from .corpus import BenchError

LOOPBACK = "127.0.0.1"
START_S = 300
"""How long the hub may take to start: its engines load and warm up."""


def hub_version() -> JsonObject:
    """The hub's version, and the commit it's built from if known, `-dirty` if it has changes
    of its own."""
    hub: JsonObject = {"version": version("tinyaisense-hub")}
    if (commit := os.environ.get("GITHUB_SHA")) is not None:
        hub["commit"] = commit[:12]
        return hub
    with contextlib.suppress(OSError, subprocess.CalledProcessError):
        head = _git("rev-parse", "HEAD")[:12]
        changed = _git("status", "--porcelain", "--untracked-files=no")
        hub["commit"] = f"{head}-dirty" if changed else head
    return hub


def _git(*args: str) -> str:
    """Raises `OSError` or `subprocess.CalledProcessError`, e.g. out of a git checkout."""
    return subprocess.run(
        ["git", *args], cwd=Path(__file__).parent, capture_output=True, text=True, check=True
    ).stdout.strip()


class HubProcess:
    """The hub that `config` makes, with an agent token of its own, serving devices and agents
    on `host` only. It listens on `ports`, for devices (TCP and UDP) and for HTTP, or on free
    ones. It starts from a copy of the `state` file if given, with the devices paired in it,
    and keeps its state, runtime files and log in a temporary dir that's gone after: if it
    fails, the end of its log goes into the `BenchError`."""

    def __init__(
        self,
        config: Config,
        *,
        host: str = LOOPBACK,
        ports: tuple[int, int] | None = None,
        state: Path | None = None,
    ) -> None:
        self._config = dataclasses.replace(config, agent_token=secrets.token_urlsafe(32))
        self._host = host
        self._ports = ports
        self._state = state
        self._dir: tempfile.TemporaryDirectory[str] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self.device_address = (host, 0)
        self.agent_url = ""

    @property
    def token(self) -> str:
        return self._config.agent_token

    async def __aenter__(self) -> Self:
        # Short, so that the owner socket's path fits a sockaddr_un.
        self._dir = tempfile.TemporaryDirectory(prefix="tas-hub-")
        try:
            await self._start(Path(self._dir.name))
        except BaseException as e:
            await self.__aexit__(type(e), e, e.__traceback__)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()
        try:
            if process is not None and isinstance(exc, _FAILURES):
                what = str(exc) if isinstance(exc, BenchError) else f"{type(exc).__name__}: {exc}"
                raise BenchError(
                    f"{what}\nThe hub, exit status {process.returncode}, logged last:\n"
                    f"{self.log_tail()}"
                ) from exc
        finally:
            if self._dir is not None:
                self._dir.cleanup()
                self._dir = None

    async def _start(self, root: Path) -> None:
        device_port, http_port = self._ports or free_ports(self._host)
        config = dataclasses.replace(self._config, device_port=device_port, http_port=http_port)
        write_config(root / "config" / APP / "hub.toml", config)
        if self._state is not None:
            (root / "state" / APP).mkdir(parents=True)
            shutil.copyfile(self._state, root / "state" / APP / "state.json")
        self.device_address = (self._host, device_port)
        self.agent_url = f"ws://{self._host}:{http_port}/agent"
        env = os.environ | {
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_RUNTIME_DIR": str(root / "run"),
        }
        (root / "run").mkdir(mode=0o700)
        with self._log.open("wb") as log:
            self._process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tinyaisense_hub",
                "serve",
                "--auto-approve",
                "--no-mdns",
                "--host",
                self._host,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        try:
            await asyncio.wait_for(self._started(), START_S)
        except TimeoutError:
            raise BenchError(f"the hub didn't start within {START_S} s") from None

    def owner(self) -> OwnerClient:
        """A client of its owner socket, to enter."""
        assert self._dir is not None
        return OwnerClient(Path(self._dir.name) / "run" / APP / SOCKET_NAME)

    async def button_latency(self) -> JsonObject:
        """The hub's own button-latency stats."""
        async with self.owner() as owner:
            return cast(JsonObject, (await owner.stats())["button_latency"])

    def log_tail(self, lines: int = 40) -> str:
        try:
            log = self._log.read_text(errors="replace")
        except FileNotFoundError:
            return "(nothing: it didn't start)"
        return "\n".join(log.splitlines()[-lines:])

    @property
    def _log(self) -> Path:
        assert self._dir is not None
        return Path(self._dir.name) / "hub.log"

    async def _started(self) -> None:
        """Wait until the hub serves its owner socket, which it opens last. Raises
        `BenchError` if it exits."""
        assert self._process is not None
        while self._process.returncode is None:
            with contextlib.suppress(OSError):
                async with self.owner():
                    return
            await asyncio.sleep(0.1)
        raise BenchError("the hub exited")


_FAILURES = (BenchError, OSError, tinyaisense.ApiError, tinyaisense.SessionEnded)
"""How the hub fails what runs it, rather than that failing itself."""
_PORTS = range(20000, 32768)
"""Where to pick the hub's ports from: below Linux's ephemeral range, so that no socket the
hub or what runs it opens meanwhile takes one before the hub listens on it."""


def free_ports(host: str = LOOPBACK) -> tuple[int, int]:
    """Two ports free on `host`: for devices, on TCP and UDP both, and for HTTP."""
    rng = random.Random()
    picked: list[int] = []
    while len(picked) < 2:
        port = rng.choice(_PORTS)
        kinds = [socket.SOCK_STREAM, socket.SOCK_DGRAM][: 2 - len(picked)]
        if port not in picked and all(_free(host, port, kind) for kind in kinds):
            picked.append(port)
    return picked[0], picked[1]


def _free(host: str, port: int, kind: socket.SocketKind) -> bool:
    with socket.socket(socket.AF_INET, kind) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True
