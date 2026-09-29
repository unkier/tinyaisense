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
import time
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Literal, cast

from tinyaisense_wire import (
    DEVICE_COMMANDS,
    KINDS,
    MDNS_SERVICE,
    Command,
    ControlDecoder,
    Error,
    FailedReply,
    Hello,
    JsonObject,
    Manifest,
    Message,
    Pending,
    Ping,
    Pong,
    Reply,
    SchemaError,
    Welcome,
    WireError,
    validate,
)

from .backoff import Backoff
from .discovery import Mdns, Resolve
from .faults import ConnectionSocket, Faults
from .led import Light, SimLed
from .profiles import PROFILES, Board
from .speaker import SimSpeaker
from .status import Status, StatusLayers

FIRMWARE = f"sim-{version('tinyaisense-sim')}"
DEAD_AFTER_MS = 6000
"""A Connection with no inbound frame for this long is dead."""

DeviceState = Literal["connecting", "pending", "online", "closed"]

_log = logging.getLogger("tinyaisense_sim")


def device_id_for(name: str) -> str:
    """`02` plus 5 bytes of a hash of the instance name, so Pairing survives across runs."""
    return "02" + hashlib.sha256(name.encode()).hexdigest()[:10]


class Simulator:
    """Runs simulated devices in this process against one hub."""

    def __init__(
        self,
        hub: str | tuple[str, int] | None = None,
        state_dir: Path | None = None,
        *,
        seed: int = 0,
        dead_after_ms: int = DEAD_AFTER_MS,
        mdns_service: str = MDNS_SERVICE,
    ) -> None:
        """`hub` is the hub's device address, `host:port`.

        Without one, devices find the hub over mDNS as `mdns_service`, as firmware does.
        Tokens live in `state_dir` if given, and only in memory otherwise. A device drops a
        Connection that brings no frame for `dead_after_ms`.
        """
        self._mdns: Mdns | None = None
        if hub is None:
            self._mdns = Mdns(mdns_service)
            self._resolve: Resolve = self._mdns.resolve
            self._udp_host = "0.0.0.0"
        else:
            address = _address(hub)
            self._resolve = lambda: _static(address)
            self._udp_host = "::" if ":" in address[0] else "0.0.0.0"
        self._tokens = _Tokens(state_dir)
        self._seed = seed
        self._dead_after = dead_after_ms / 1000
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
        profile = PROFILES[board]
        device = SimDevice(
            name,
            profile,
            profile.manifest(device_id_for(name), FIRMWARE),
            self._resolve,
            self._udp_host,
            self._tokens,
            # Its own stream, so that one device's randomness doesn't shift another's.
            random.Random(f"{self._seed}:{name}"),
            self._dead_after,
            self._devices.remove,
        )
        self._devices.append(device)
        await device.start()
        await device.wait_for(lambda: device.state in ("pending", "online"))
        return device

    async def close(self) -> None:
        for device in list(self._devices):
            await device.close()
        if self._mdns is not None:
            await self._mdns.close()


class SimDevice:
    """One simulated device. Get one from `Simulator.add`."""

    def __init__(
        self,
        instance: str,
        board: Board,
        manifest: Manifest,
        resolve: Resolve,
        udp_host: str,
        tokens: _Tokens,
        rng: random.Random,
        dead_after: float,
        on_close: Callable[[SimDevice], None],
    ) -> None:
        self.instance = instance
        self.manifest = manifest
        self.state: DeviceState = "connecting"
        self.name: str | None = None
        """The hub-owned name from the last `welcome`."""
        self.last_error: str | None = None
        """The `code` of the last `error` from the hub."""
        self.conn_id: int | None = None
        """From `welcome`, while the Connection lasts."""
        self.reconnects = 0
        """Connections the hub answered, after the first."""
        self.faults = Faults()
        self._board = board
        self._resolve = resolve
        self._udp_host = udp_host
        self._tokens = tokens
        self._backoff = Backoff(rng)
        self._dead_after = dead_after
        self._on_close = on_close
        self._boot_ns = time.monotonic_ns()
        self._changed = asyncio.Event()
        self._udp: asyncio.DatagramTransport | None = None
        self._task: asyncio.Task[None] | None = None
        self._answered_before = False
        """Whether the hub has answered any Connection yet."""
        self._connections_lost = 0
        """Connections that ended since boot, answered or not: `diag` reports it."""
        self._vanished: list[ConnectionSocket] = []
        """Given up on, but left open: closing one would tell the hub."""
        self._layers = StatusLayers(connecting=True)
        self._identify_timer: asyncio.TimerHandle | None = None
        """Ends `identify`, while it shows."""
        self._leds = {
            cap.id: SimLed(cap.params["count"], board.led_brightness_cap)
            for cap in manifest.capabilities
            if cap.kind == "led"
        }
        self._status_led = next(iter(self._leds.values()), None)
        """The LED the Status shows on: the first one."""
        self._speakers = {
            cap.id: SimSpeaker(board.default_volume)
            for cap in manifest.capabilities
            if cap.kind == "speaker"
        }

    @property
    def device_id(self) -> str:
        return self.manifest.device_id

    @property
    def token(self) -> str | None:
        return self._tokens.get(self.device_id)

    @property
    def status(self) -> Status:
        """The Status the device shows now."""
        return self._layers.shown

    @property
    def speaker(self) -> SimSpeaker:
        """The speaker capability, on a Board with one."""
        [speaker] = self._speakers.values()
        return speaker

    def lights(self, cap: str = "led") -> tuple[Light, ...]:
        """What each LED of the led capability `cap` shows now, the Status included."""
        led = self._leds[cap]
        return led.lights(self.status if led is self._status_led else None)

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        """Wait until `predicate()` holds. It is checked on every change of this device."""
        while not predicate():
            await self._changed.wait()

    async def start(self) -> None:
        # The UDP socket is bound before the first `hello`, which announces its port.
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=(self._udp_host, 0)
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
        if self._identify_timer is not None:
            self._identify_timer.cancel()
        self._close_vanished()
        self._set_state("closed")
        self._on_close(self)

    def _changed_now(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def _set_state(self, state: DeviceState) -> None:
        self.state = state
        self._layers.connecting = state == "connecting"
        self._layers.pending = state == "pending"
        self._changed_now()

    def _close_vanished(self) -> None:
        for connection in self._vanished:
            connection.close()
        self._vanished.clear()

    async def _run(self) -> None:
        hub: tuple[str, int] | None = None
        while True:
            try:
                if hub is None:
                    hub = await self._resolve()
                await self._connection(hub)
            except (OSError, WireError) as e:
                _log.info("%s: connection failed: %s", self.instance, e)
            self.faults.connection = None
            self.conn_id = None
            self._idle()
            self._set_state("connecting")
            retry = self._backoff.failed()
            await asyncio.sleep(retry.delay)
            if retry.resolve:
                hub = None

    async def _connection(self, hub: tuple[str, int]) -> None:
        """One Connection, from connect to drop."""
        assert self._udp is not None
        udp_port = cast(tuple[str, int], self._udp.get_extra_info("sockname"))[1]
        connection = ConnectionSocket(*await asyncio.open_connection(*hub))
        self.faults.connection = connection
        try:
            connection.raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.send(Hello(self.token, udp_port, self.manifest))
            decoder = ControlDecoder()
            answered = False
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._dead_after
            while True:
                try:
                    async with asyncio.timeout_at(deadline):
                        data = await connection.reader.read(65536)
                        # A fault that struck while the read was pending holds what it got.
                        await self.faults.readable(connection)
                except TimeoutError:
                    _log.info("%s: the hub went silent", self.instance)
                    return
                if not data:
                    break
                for message in decoder.feed(data):
                    deadline = loop.time() + self._dead_after
                    if isinstance(message, (Pending, Welcome)) and not answered:
                        answered = True
                        self._answered(connection)
                    self._receive(message, connection)
            decoder.close()
        finally:
            self._connections_lost += 1
            if connection.vanished:
                self._vanished.append(connection)
            else:
                connection.close()

    def _answered(self, connection: ConnectionSocket) -> None:
        """The hub answered this Connection's `hello`, with `pending` or `welcome`."""
        if self._answered_before:
            self.reconnects += 1
        self._answered_before = True
        # This hello superseded any Connection the device gave up on, so the hub has closed
        # those by now, and closing them tells it nothing.
        self._close_vanished()

    def _receive(self, message: Message, connection: ConnectionSocket) -> None:
        match message:
            case Ping():
                connection.send(Pong(message.t, self._clock()))
            case Pending():
                self._set_state("pending")
            case Welcome():
                if message.token is not None:
                    self._tokens.set(self.device_id, message.token)
                self.name = message.name
                self.conn_id = message.conn_id
                self._backoff.welcomed()
                self._layers.hub = "no_agent"
                self._set_state("online")
            case Command() if self.state == "online":
                reply = self._command(message)
                if delay := self.faults.delay_replies_ms:
                    asyncio.get_running_loop().call_later(delay / 1000, connection.send, reply)
                else:
                    connection.send(reply)
                self._changed_now()
            case Error():
                self.last_error = message.code
                if message.code == "unauthorized":
                    # Wipe the token, so the next Connection shows up pending.
                    self._tokens.set(self.device_id, None)
                self._changed_now()
            case _:
                pass

    def _idle(self) -> None:
        """The Connection is over: every capability goes idle."""
        for led in self._leds.values():
            led.release()
        self._layers.claimed = False

    def _command(self, command: Command) -> Reply | FailedReply:
        if (code := self.faults.next_failure()) is not None:
            return FailedReply(command.id, code, "the fail_next fault")
        try:
            result = self._apply(command)
        except SchemaError as e:
            return FailedReply(command.id, "invalid_args", str(e))
        except _Refused as e:
            return FailedReply(command.id, e.code, e.message)
        return Reply(command.id, result)

    def _apply(self, command: Command) -> JsonObject:
        """Raises `SchemaError` or `_Refused`."""
        name, args = command.name, command.args
        if command.cap is None:
            contract = DEVICE_COMMANDS.get(name)
            kind = "device"
        else:
            cap = next((c for c in self.manifest.capabilities if c.id == command.cap), None)
            if cap is None:
                raise _Refused("unsupported", f"no capability {command.cap!r}")
            if cap.kind not in KINDS:
                raise _Refused("unsupported", f"{cap.id!r} is of an unknown Kind, {cap.kind!r}")
            contract = KINDS[cap.kind].commands.get(name)
            kind = cap.kind
        if contract is None:
            raise _Refused("unsupported", f"{kind} has no command {name!r}")
        validate(contract.args, args, "args")
        result: JsonObject = {}
        match kind, name:
            case "device", "status":
                self._layers.hub = args["state"]
            case "device", "identify":
                self._identify(seconds=args.get("s", 5))
            case "device", "diag":
                result = self._diag()
            case "speaker", "set_volume":
                assert command.cap is not None
                self._speakers[command.cap].level = args["level"]
            case "led", "set":
                assert command.cap is not None
                try:
                    self._leds[command.cap].set(args)
                except ValueError as e:
                    raise _Refused("invalid_args", str(e)) from None
            case "led", "off":
                assert command.cap is not None
                self._leds[command.cap].off()
            case "led", "release":
                assert command.cap is not None
                self._leds[command.cap].release()
            case _:
                raise _Refused("unsupported", f"the Simulator doesn't do {kind} {name!r} yet")
        self._layers.claimed = self._status_led is not None and self._status_led.claim is not None
        return result

    def _identify(self, seconds: int) -> None:
        """Show `identify` for `seconds` from now, cutting short any earlier one."""
        if self._identify_timer is not None:
            self._identify_timer.cancel()
        self._layers.identify = True
        self._identify_timer = asyncio.get_running_loop().call_later(seconds, self._identified)

    def _identified(self) -> None:
        self._identify_timer = None
        self._layers.identify = False
        self._changed_now()

    def _diag(self) -> JsonObject:
        health = self._board.health
        return {
            "uptime_s": self._clock() // 1_000_000,
            "reset_reason": "poweron",
            "heap_free": health.heap_free,
            "heap_min": health.heap_min,
            "rssi": health.rssi,
            "reconnects": self._connections_lost,
            "stack_hwm": dict(health.stack_hwm),
        }

    def _clock(self) -> int:
        """The device clock: µs since boot."""
        return (time.monotonic_ns() - self._boot_ns) // 1000


class _Refused(Exception):
    """The device refuses a command with an error `reply`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


async def _static(address: tuple[str, int]) -> tuple[str, int]:
    return address


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
