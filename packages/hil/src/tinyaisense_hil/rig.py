"""The rig config, `hil.toml`, which git ignores: the Echo's serial port, and what to reach the
Echo by. Only `port` is needed: the rest is found from the Echo's console and the route to it.

    port = "/dev/ttyUSB0"           # the Echo's serial console
    echo_ip = "192.168.0.103"       # default: what the console's `status` says
    iface = "enp5s0"                # default: the route's, to the Echo
    hub_ip = "192.168.0.106"        # default: the route's source address
    state = "~/.local/state/tinyaisense/state.json"  # the hub state the Echo is paired in
    netem = ["sudo", "-n", "/usr/local/sbin/tinyaisense-netem"]  # packages/hil/netem
    pio = "pio"                     # PlatformIO, to build and flash the debug build
    soak_minutes = 60
"""

import ipaddress
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NETEM = ("sudo", "-n", "/usr/local/sbin/tinyaisense-netem")
"""The netem wrapper, as `packages/hil/netem/install.sh` installs it: through a sudo rule for it
alone."""
FILE = "hil.toml"


class RigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Rig:
    port: str
    echo_ip: str | None = None
    iface: str | None = None
    hub_ip: str | None = None
    state: str | None = None
    """The hub state the Echo is paired in, which the HIL hub starts from a copy of, so that
    the Echo keeps its token. Default: the owner's hub's."""
    netem: tuple[str, ...] = NETEM
    pio: str = "pio"
    soak_minutes: float = 60


def find_rig() -> Path:
    """Where the rig config is: env `TINYAISENSE_HIL_RIG`, else `hil.toml` here, else in the
    config dir, `~/.config/tinyaisense/`. With none, `hil.toml` here: where to put one."""
    if (named := os.environ.get("TINYAISENSE_HIL_RIG")) is not None:
        return Path(named)
    here = Path(FILE)
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    configured = config / "tinyaisense" / FILE
    return configured if not here.exists() and configured.exists() else here.absolute()


def load_rig(path: Path) -> Rig:
    """Raises `RigError`."""
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise RigError(
            f"no rig config at {path}: write one with at least the Echo's serial port, "
            f'port = "/dev/ttyUSB0"'
        ) from None
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise RigError(f"{path}: {e}") from None
    settings = _Settings(raw)
    rig = Rig(
        port=settings.string("port"),
        echo_ip=settings.ip("echo_ip"),
        iface=settings.optional_string("iface"),
        hub_ip=settings.ip("hub_ip"),
        state=settings.path("state"),
        netem=settings.command("netem", NETEM),
        pio=settings.path("pio") or "pio",
        soak_minutes=settings.positive("soak_minutes", 60),
    )
    settings.done()
    return rig


class _Settings:
    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw
        self._seen: set[str] = set()

    def _get(self, key: str) -> Any:
        self._seen.add(key)
        return self._raw.get(key)

    def string(self, key: str) -> str:
        value = self._get(key)
        if not isinstance(value, str) or not value:
            raise RigError(f"{key} must be a string")
        return value

    def optional_string(self, key: str) -> str | None:
        return None if self._get(key) is None else self.string(key)

    def path(self, key: str) -> str | None:
        value = self.optional_string(key)
        return None if value is None else os.path.expanduser(value)

    def ip(self, key: str) -> str | None:
        value = self.optional_string(key)
        if value is not None:
            try:
                ipaddress.IPv4Address(value)
            except ValueError:
                raise RigError(f"{key} must be an IPv4 address, not {value!r}") from None
        return value

    def command(self, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = self._get(key)
        if value is None:
            return default
        args: list[object] = value if isinstance(value, list) else []  # pyright: ignore[reportUnknownVariableType]
        command = tuple(arg for arg in args if isinstance(arg, str) and arg)
        if not command or len(command) != len(args):
            raise RigError(f"{key} must be a command as a list of strings")
        return command

    def positive(self, key: str, default: float) -> float:
        value = self._get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise RigError(f"{key} must be a positive number")
        return value

    def done(self) -> None:
        if unknown := sorted(set(self._raw) - self._seen):
            raise RigError(f"unknown settings in the rig config: {', '.join(unknown)}")
