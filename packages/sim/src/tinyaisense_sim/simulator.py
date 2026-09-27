"""Simulated devices, each speaking the device protocol to a real hub as its firmware would."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import socket
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Literal, cast

from tinyaisense_wire import (
    ControlDecoder,
    Error,
    Hello,
    Manifest,
    Message,
    Pending,
    Welcome,
    WireError,
    encode_control,
)

from .profiles import PROFILES

FIRMWARE = f"sim-{version('tinyaisense-sim')}"
BACKOFF_S = (0.5, 1.0, 2.0, 5.0)
"""The reconnect backoff: one step per failure in a row, capped at the last."""
BACKOFF_JITTER = 0.2

DeviceState = Literal["connecting", "pending", "online", "closed"]

_log = logging.getLogger("tinyaisense_sim")


def device_id_for(name: str) -> str:
    """`02` plus 5 bytes of a hash of the instance name, so Pairing survives across runs."""
    return "02" + hashlib.sha256(name.encode()).hexdigest()[:10]


class Simulator:
    """Runs simulated devices in this process against one hub."""

    def __init__(
        self, hub: str | tuple[str, int], state_dir: Path | None = None, *, seed: int = 0
    ) -> None:
        """`hub` is the hub's device address, `host:port`.

        Tokens live in `state_dir` if given, and only in memory otherwise.
        """
        self._address = _address(hub)
        self._tokens = _Tokens(state_dir)
        self._rng = random.Random(seed)
        self._devices: list[SimDevice] = []

    @property
    def devices(self) -> tuple[SimDevice, ...]:
        return tuple(self._devices)

    async def add(self, board: str, name: str) -> SimDevice:
        """Start a simulated `board` device, identified by its instance `name`.

        Returns once the hub has answered it with `welcome` or `pending`. Until then the
        device retries as firmware would, forever: bound the wait with `asyncio.timeout`.
        """
        if board not in PROFILES:
            raise ValueError(f"unknown board {board!r}; known: {', '.join(PROFILES)}")
        manifest = PROFILES[board](device_id_for(name), FIRMWARE)
        device = SimDevice(
            name, manifest, self._address, self._tokens, self._rng, self._devices.remove
        )
        self._devices.append(device)
        await device.start()
        await device.wait_for(lambda: device.state in ("pending", "online"))
        return device

    async def close(self) -> None:
        for device in list(self._devices):
            await device.close()


class SimDevice:
    """One simulated device. Get one from `Simulator.add`."""

    def __init__(
        self,
        instance: str,
        manifest: Manifest,
        hub: tuple[str, int],
        tokens: _Tokens,
        rng: random.Random,
        on_close: Callable[[SimDevice], None],
    ) -> None:
        self.instance = instance
        self.manifest = manifest
        self.state: DeviceState = "connecting"
        self.name: str | None = None
        """The hub-owned name from the last `welcome`."""
        self.last_error: str | None = None
        """The `code` of the last `error` from the hub."""
        self._hub = hub
        self._tokens = tokens
        self._rng = rng
        self._on_close = on_close
        self._failures = 0
        self._changed = asyncio.Event()
        self._udp: asyncio.DatagramTransport | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def device_id(self) -> str:
        return self.manifest.device_id

    @property
    def token(self) -> str | None:
        return self._tokens.get(self.device_id)

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        """Wait until `predicate()` holds. It is checked on every change of this device."""
        while not predicate():
            await self._changed.wait()

    async def start(self) -> None:
        # The UDP socket is bound before the first `hello`, which announces its port.
        host = self._hub[0]
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("::" if ":" in host else "0.0.0.0", 0)
        )
        self._udp = transport
        self._task = asyncio.create_task(self._run(), name=f"sim device {self.instance}")

    async def close(self) -> None:
        """Power the device off."""
        if self.state == "closed":
            return
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._udp is not None:
            self._udp.close()
        self._set_state("closed")
        self._on_close(self)

    def _changed_now(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def _set_state(self, state: DeviceState) -> None:
        self.state = state
        self._changed_now()

    async def _run(self) -> None:
        while True:
            try:
                await self._connection()
            except (OSError, WireError) as e:
                _log.info("%s: connection failed: %s", self.instance, e)
            self._set_state("connecting")
            step = BACKOFF_S[min(self._failures, len(BACKOFF_S) - 1)]
            self._failures += 1
            await asyncio.sleep(step * (1 + self._rng.uniform(0, BACKOFF_JITTER)))

    async def _connection(self) -> None:
        """One Connection, from connect to drop."""
        assert self._udp is not None
        udp_port = cast(tuple[str, int], self._udp.get_extra_info("sockname"))[1]
        reader, writer = await asyncio.open_connection(*self._hub)
        try:
            sock = cast(socket.socket, writer.get_extra_info("socket"))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            writer.write(encode_control(Hello(self.token, udp_port, self.manifest)))
            decoder = ControlDecoder()
            while data := await reader.read(65536):
                for message in decoder.feed(data):
                    self._receive(message)
            decoder.close()
        finally:
            writer.close()

    def _receive(self, message: Message) -> None:
        match message:
            case Pending():
                self._set_state("pending")
            case Welcome():
                if message.token is not None:
                    self._tokens.set(self.device_id, message.token)
                self.name = message.name
                self._failures = 0
                self._set_state("online")
            case Error():
                self.last_error = message.code
                if message.code == "unauthorized":
                    # Wipe the token, so the next Connection shows up pending.
                    self._tokens.set(self.device_id, None)
                self._changed_now()
            case _:
                pass


def _address(hub: str | tuple[str, int]) -> tuple[str, int]:
    if isinstance(hub, tuple):
        return hub
    host, _, port = hub.rpartition(":")
    return host.strip("[]"), int(port)


class _Tokens:
    """Device tokens by `device_id`: in memory, and in `<state_dir>/tokens.json` if given."""

    def __init__(self, state_dir: Path | None) -> None:
        self._path = None if state_dir is None else state_dir / "tokens.json"
        self._tokens: dict[str, str] = {}
        if self._path is not None and self._path.exists():
            self._tokens = cast(dict[str, str], json.loads(self._path.read_text()))

    def get(self, device_id: str) -> str | None:
        return self._tokens.get(device_id)

    def set(self, device_id: str, token: str | None) -> None:
        if token is None:
            self._tokens.pop(device_id, None)
        else:
            self._tokens[device_id] = token
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                f.write(json.dumps(self._tokens))
            os.replace(tmp, self._path)
