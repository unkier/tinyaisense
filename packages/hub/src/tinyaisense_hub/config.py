"""The hub config (TOML, read at start) and where the hub keeps its files."""

import os
import secrets
import tempfile
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, cast

from tinyaisense_wire import DEVICE_PORT, HTTP_PORT

APP = "tinyaisense"


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Timing:
    """Every hub timeout, in ms: the `[timing]` section."""

    hello_timeout_ms: int = 5000
    """A device or an agent link must send `hello` this soon after it connects."""
    ping_interval_ms: int = 2000
    """The hub pings each device this often, from its `hello` on."""
    dead_after_ms: int = 6000
    """A Connection with no inbound frame for this long is dead. More than the ping interval."""
    clock_window_ms: int = 20000
    """The device-clock offset comes from the shortest round trip among the pongs this recent."""
    agent_ping_interval_ms: int = 2000
    """The hub pings each agent link this often."""
    agent_dead_after_ms: int = 6000
    """An agent link with no inbound frame for this long is dead, and its Session ends."""
    speaker_buffer_ms: int = 300_000
    """How far ahead of playback an agent may send speaker audio. Past it, the stream ends
    with `overflow`."""


@dataclass(frozen=True, slots=True)
class Config:
    agent_token: str
    device_port: int = DEVICE_PORT
    """TCP and UDP. 0 picks free ports."""
    http_port: int = HTTP_PORT
    timing: Timing = field(default_factory=Timing)


def new_config() -> Config:
    return Config(agent_token=secrets.token_urlsafe(32))


def render_config(config: Config) -> str:
    timing = "\n".join(f"{f.name} = {getattr(config.timing, f.name)}" for f in fields(Timing))
    return f"""\
# tinyaisense hub config. The hub reads it at start: restart the hub to apply changes.

# Agents authenticate with this token.
agent_token = "{config.agent_token}"

[ports]
# Device protocol, TCP and UDP.
device = {config.device_port}
# HTTP: /agent and /mcp.
http = {config.http_port}

# Every hub timeout, in ms.
[timing]
{timing}
"""


def parse_config(text: str) -> Config:
    """Raises `ConfigError`."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(str(e)) from None
    top = _Table(doc, "")
    ports = top.table("ports")
    timing = top.table("timing")
    config = Config(
        agent_token=top.string("agent_token"),
        device_port=ports.port("device", DEVICE_PORT),
        http_port=ports.port("http", HTTP_PORT),
        timing=Timing(
            **{f.name: timing.positive_int(f.name, cast(int, f.default)) for f in fields(Timing)}
        ),
    )
    for table in (top, ports, timing):
        table.check_unknown()
    if config.timing.dead_after_ms <= config.timing.ping_interval_ms:
        raise ConfigError("timing.dead_after_ms must be more than timing.ping_interval_ms")
    if config.timing.agent_dead_after_ms <= config.timing.agent_ping_interval_ms:
        raise ConfigError(
            "timing.agent_dead_after_ms must be more than timing.agent_ping_interval_ms"
        )
    return config


def load_config(path: Path) -> Config:
    """Raises `ConfigError`."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ConfigError(f"no config at {path}: run `tinyaisense-hub init` first") from None
    try:
        return parse_config(text)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from None


def write_config(path: Path, config: Config) -> None:
    """Write a new config, readable by the owner only. Raises `FileExistsError`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(render_config(config))


def config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / APP / "hub.toml"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP


def runtime_dir() -> Path:
    base = _xdg("XDG_RUNTIME_DIR", None)
    if base is None:
        return Path(tempfile.gettempdir()) / f"{APP}-{os.getuid()}"
    return base / APP


def _xdg[T: Path | None](variable: str, default: T) -> Path | T:
    value = os.environ.get(variable, "")
    # The XDG spec says to ignore relative paths.
    return Path(value) if os.path.isabs(value) else default


class _Table:
    """Typed access to a TOML table. Raises `ConfigError`."""

    def __init__(self, table: dict[str, Any], where: str) -> None:
        self._table = table
        self._where = where
        self._read: set[str] = set()

    def _key(self, key: str) -> str:
        return f"{self._where}.{key}" if self._where else key

    def _get(self, key: str) -> object:
        self._read.add(key)
        return self._table.get(key)

    def table(self, key: str) -> _Table:
        value = self._get(key)
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ConfigError(f"[{self._key(key)}] must be a table")
        return _Table(cast(dict[str, Any], value), self._key(key))

    def string(self, key: str) -> str:
        value = self._get(key)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{self._key(key)} must be a non-empty string")
        return value

    def port(self, key: str, default: int) -> int:
        value = self._get(key)
        if value is None:
            return default
        if type(value) is not int or not 0 <= value <= 65535:
            raise ConfigError(f"{self._key(key)} must be a port number, 0..65535")
        return value

    def positive_int(self, key: str, default: int) -> int:
        value = self._get(key)
        if value is None:
            return default
        if type(value) is not int or value <= 0:
            raise ConfigError(f"{self._key(key)} must be a positive integer")
        return value

    def check_unknown(self) -> None:
        if unknown := set(self._table) - self._read:
            raise ConfigError(f"unknown keys: {', '.join(sorted(map(self._key, unknown)))}")
