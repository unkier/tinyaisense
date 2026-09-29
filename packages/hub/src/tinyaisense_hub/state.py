"""The hub's persistent state: paired devices, as JSON written by atomic rename."""

import asyncio
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from tinyaisense_wire import Manifest, WireError


class StateError(Exception):
    pass


@dataclass(slots=True)
class DeviceRecord:
    """A paired device."""

    device_id: str
    name: str
    token_sha256: str
    """The hub never stores the token itself."""
    manifest: Manifest
    """The last Manifest the device sent."""
    last_seen: int
    """Epoch ms: when its last Connection started or ended."""
    volume: int | None = None
    """The owner's speaker volume, 0 to 100. None until set: the device keeps its default."""

    def to_json(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "token_sha256": self.token_sha256,
            "manifest": self.manifest.to_json(),
            "last_seen": self.last_seen,
            "volume": self.volume,
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> DeviceRecord:
        volume = obj.get("volume")
        if volume is not None and (type(volume) is not int or not 0 <= volume <= 100):
            raise ValueError(f"volume {volume!r} isn't 0 to 100")
        return DeviceRecord(
            obj["device_id"],
            obj["name"],
            obj["token_sha256"],
            Manifest.from_json(obj["manifest"]),
            obj["last_seen"],
            volume,
        )


class StateStore:
    """`<state_dir>/state.json`. Saves land in the order they're asked for."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "state.json"
        self._lock = asyncio.Lock()

    def load(self) -> dict[str, DeviceRecord]:
        """Raises `StateError`."""
        try:
            doc = cast(dict[str, Any], json.loads(self.path.read_text()))
            records = [DeviceRecord.from_json(obj) for obj in doc["devices"]]
        except FileNotFoundError:
            return {}
        except (ValueError, KeyError, TypeError, WireError) as e:
            raise StateError(f"{self.path} is broken: {e!r}") from None
        return {record.device_id: record for record in records}

    async def save(self, records: Iterable[DeviceRecord]) -> None:
        data = json.dumps({"devices": [record.to_json() for record in records]}, indent=1)
        async with self._lock:
            await asyncio.to_thread(_write_atomic, self.path, data.encode())


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
