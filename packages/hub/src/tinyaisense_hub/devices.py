"""Device Connections, the handshake, Pairing and the owner's device ops."""

import asyncio
import hashlib
import logging
import re
import secrets
import socket
import time
from collections.abc import Callable, Coroutine
from typing import Any, Literal, Protocol, cast

from tinyaisense_wire import (
    DEVICE_COMMANDS,
    KINDS,
    PROTOCOL,
    Capability,
    Command,
    CommandContract,
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
    encode_control,
    validate,
)

from .commands import Answered, CommandTimeouts, Replied, Sent, TimedOut
from .config import Timing
from .errors import HubError, HubErrorCode
from .keepalive import ClockSync, Dead, Inbound, Keepalive, SendPing, Tick
from .state import DeviceRecord, StateStore

NAME = re.compile("[a-z0-9-]+")
"""Device names."""

_log = logging.getLogger("tinyaisense_hub.devices")

ConnectionState = Literal["hello", "pending", "pairing", "online", "closed"]

DEFAULT_TIMEOUT_MS = 1000
"""For a command whose contract declares no timeout."""
_DEVICE_ERRORS: set[HubErrorCode] = {"unsupported", "invalid_args", "busy", "failed"}
"""The error codes a device may reply with. Anything else counts as `failed`."""
_U32_MAX = 2**32 - 1

type CommandSpec = tuple[str | None, str, JsonObject]
"""A command to send: `(cap, name, args)`, with `cap` None for a device-level one."""


class Presence(Protocol):
    """Told when a paired device's Connection is welcomed, and when it drops."""

    def device_online(self, conn: DeviceConnection, record: DeviceRecord) -> None: ...

    def device_offline(self, conn: DeviceConnection) -> None: ...


class _Alarm:
    """One loop timer, set for the earliest deadline asked of it.

    Times are µs on the hub's monotonic clock. A deadline that moves later leaves the timer
    early: it finds nothing due and asks for the next one.
    """

    def __init__(self, due: Callable[[int], None]) -> None:
        self._due = due
        self._timer: asyncio.TimerHandle | None = None
        self._at = 0

    def set(self, deadline: int | None, now: int) -> None:
        if deadline is not None and (self._timer is None or deadline < self._at):
            self.cancel()
            self._at = deadline
            self._timer = asyncio.get_running_loop().call_later(
                max(0, deadline - now) / 1_000_000, self._fire
            )

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _fire(self) -> None:
        self._timer = None
        self._due(_now_us())


class DeviceConnection(asyncio.Protocol):
    """One device's Connection, from TCP accept to drop. Every handler is synchronous."""

    def __init__(self, registry: Registry, timing: Timing) -> None:
        self.state: ConnectionState = "hello"
        self.hello: Hello | None = None
        self.conn_id = 0
        """Set by `welcome`."""
        self.name = ""
        """The device name `welcome` gave it."""
        self.clock = ClockSync(window=timing.clock_window_ms * 1000)
        """The device clock, from its pongs."""
        self._registry = registry
        self._timing = timing
        self._decoder = ControlDecoder()
        self._transport: asyncio.Transport | None = None
        self._hello_timer: asyncio.TimerHandle | None = None
        self._keepalive: Keepalive | None = None
        """Set by `hello`."""
        self._keepalive_alarm = _Alarm(self._run_keepalive)
        self._timeouts = CommandTimeouts()
        self._timeouts_alarm = _Alarm(self._run_timeouts)
        self._in_flight: dict[int, asyncio.Future[JsonObject]] = {}
        self._next_command_id = 1
        self._peer = "?"

    def __str__(self) -> str:
        return self._peer if self.hello is None else self.manifest.device_id

    @property
    def manifest(self) -> Manifest:
        assert self.hello is not None
        return self.hello.manifest

    @property
    def last_seen(self) -> int:
        """Epoch ms of the last inbound frame, from the `hello` on."""
        assert self._keepalive is not None
        return _now_ms() - (_now_us() - self._keepalive.last_inbound) // 1000

    def send(self, message: Message) -> None:
        if self._transport is not None and self.state != "closed":
            self._transport.write(encode_control(message))

    def command(self, cap: str | None, name: str, args: JsonObject) -> asyncio.Future[JsonObject]:
        """Send a command to capability `cap`, or to the device itself if None, at once.

        The future holds the reply's result, or fails with `HubError`: the device's own error,
        `timeout`, or `disconnected`. The timeout is the command's own, from its contract.
        """
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        if self.state != "online":
            future.set_exception(HubError("disconnected", f"{self.name or self} is offline"))
            return future
        command_id = self._next_command_id
        self._next_command_id = command_id % _U32_MAX + 1
        self._in_flight[command_id] = future
        self.send(Command(command_id, cap, name, args))
        now = _now_us()
        self._timeouts.handle(Sent(command_id, self._timeout_ms(cap, name) * 1000), now)
        self._timeouts_alarm.set(self._timeouts.next_deadline(), now)
        return future

    async def pipeline(self, commands: list[CommandSpec], *, strict: bool = True) -> None:
        """Send `commands` back to back, then await every reply. Raises the first failure, if
        `strict`; logs them otherwise."""
        replies = [self.command(cap, name, args) for cap, name, args in commands]
        results = await asyncio.gather(*replies, return_exceptions=True)
        failures = [r for r in results if isinstance(r, BaseException)]
        for failure in failures if not strict else []:
            _log.warning("%s: %s", self.name, failure)
        if strict and failures:
            raise failures[0]

    def close(self, reason: str) -> None:
        """Close the Connection. What was sent before goes out first."""
        if self._teardown(reason) and self._transport is not None:
            self._transport.close()

    def abort(self, reason: str) -> None:
        """Drop the Connection at once, unsent data and all: the device isn't listening."""
        if self._teardown(reason) and self._transport is not None:
            self._transport.abort()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = cast(asyncio.Transport, transport)
        self._peer = str(transport.get_extra_info("peername"))
        sock = cast(socket.socket, transport.get_extra_info("socket"))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._hello_timer = asyncio.get_running_loop().call_later(
            self._timing.hello_timeout_ms / 1000, self.close, "no hello in time"
        )

    def data_received(self, data: bytes) -> None:
        now = _now_us()
        try:
            for message in self._decoder.feed(data):
                if self.state == "closed":
                    return
                if self._keepalive is not None:
                    self._keepalive.handle(Inbound(), now)
                self._receive(message, now)
        except WireError as e:
            if e.code == "protocol_mismatch" and self.state == "hello":
                self.send(Error("protocol_mismatch", e.detail, PROTOCOL))
            self.close(f"protocol error: {e}")

    def eof_received(self) -> None:
        try:
            self._decoder.close()
        except WireError as e:
            _log.info("%s: %s", self, e)

    def connection_lost(self, exc: Exception | None) -> None:
        self._teardown("dropped" if exc is None else f"dropped: {exc}")

    def _receive(self, message: Message, now: int) -> None:
        match message:
            case Pong():
                self.clock.handle(message, now)
            case Ping():
                pass
            case Hello() if self.state == "hello":
                self._stop_timer(self._hello_timer)
                self.hello = message
                self._keepalive = Keepalive(
                    now,
                    ping_interval=self._timing.ping_interval_ms * 1000,
                    dead_after=self._timing.dead_after_ms * 1000,
                )
                self._registry.hello(self)
                self._run_keepalive(now)
            case Hello():
                self.close("a second hello")
            case _ if self.state != "online":
                self.close(f"{type(message).__name__} before welcome")
            case Reply() | FailedReply():
                self._reply(message, now)
            case _:
                pass

    def _reply(self, reply: Reply | FailedReply, now: int) -> None:
        for action in self._timeouts.handle(Replied(reply.id), now):
            future = self._in_flight.pop(action.id)
            match action, reply:
                case Answered(), Reply():
                    _resolve(future, reply.result)
                case Answered(), FailedReply():
                    code = reply.code if reply.code in _DEVICE_ERRORS else "failed"
                    _fail(future, HubError(code, reply.message))
                case TimedOut(), _:
                    self._timed_out(future)

    def _run_timeouts(self, now: int) -> None:
        for action in self._timeouts.handle(Tick(), now):
            self._timed_out(self._in_flight.pop(action.id))
        if self.state != "closed":
            self._timeouts_alarm.set(self._timeouts.next_deadline(), now)

    def _timed_out(self, future: asyncio.Future[JsonObject]) -> None:
        _fail(future, HubError("timeout", f"{self.name} didn't reply in time"))

    def _timeout_ms(self, cap: str | None, name: str) -> int:
        if cap is None:
            contract = DEVICE_COMMANDS.get(name)
        else:
            capability = next((c for c in self.manifest.capabilities if c.id == cap), None)
            kind = KINDS.get(capability.kind) if capability is not None else None
            contract = kind.commands.get(name) if kind is not None else None
        return DEFAULT_TIMEOUT_MS if contract is None else contract.timeout_ms

    def _run_keepalive(self, now: int) -> None:
        """Do what the keepalive has due, and set the timer for its next deadline."""
        assert self._keepalive is not None
        for action in self._keepalive.handle(Tick(), now):
            match action:
                case SendPing(t):
                    self.send(Ping(t))
                case Dead():
                    self.abort(f"dead: no frame for {self._timing.dead_after_ms} ms")
        if self.state != "closed":
            self._keepalive_alarm.set(self._keepalive.next_deadline(), now)

    def _teardown(self, reason: str) -> bool:
        """Release everything the Connection holds, once. Returns whether this call did."""
        if self.state == "closed":
            return False
        _log.info("%s: Connection closed: %s", self, reason)
        self.state = "closed"
        self._stop_timer(self._hello_timer)
        self._hello_timer = None
        self._keepalive_alarm.cancel()
        self._timeouts_alarm.cancel()
        in_flight, self._in_flight = self._in_flight, {}
        for future in in_flight.values():
            _fail(future, HubError("disconnected", f"{self.name} dropped: {reason}"))
        self._registry.detach(self)
        return True

    @staticmethod
    def _stop_timer(timer: asyncio.TimerHandle | None) -> None:
        if timer is not None:
            timer.cancel()


class Registry:
    """Paired devices, pending devices and their live Connections. Owns Pairing and the
    owner's device ops.

    A device is pending while it has an open Connection without a token. Pending devices
    live in memory only. The owner's device ops go straight to the Connection, so no agent op
    queued on the device holds them up.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        udp_port: int,
        timing: Timing,
        auto_approve: bool,
        presence: Presence,
    ) -> None:
        self._store = store
        self._records = store.load()
        self._live: dict[str, DeviceConnection] = {}
        """device_id → its pending or online Connection."""
        self._connections: set[DeviceConnection] = set()
        """Every open Connection, from accept on."""
        self._udp_port = udp_port
        self._timing = timing
        self._auto_approve = auto_approve
        self._presence = presence
        self._tasks: set[asyncio.Task[Any]] = set()

    def connection(self) -> DeviceConnection:
        """The protocol factory for the device TCP server."""
        conn = DeviceConnection(self, self._timing)
        self._connections.add(conn)
        return conn

    async def close(self) -> None:
        """Close every Connection, then wait for background saves."""
        for conn in list(self._connections):
            conn.close("the hub is shutting down")
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # Owner operations.

    def devices(self) -> list[JsonObject]:
        """Paired and pending devices, by device_id. `last_seen` is in epoch ms."""
        live = {d: c for d, c in self._live.items() if c.state != "closed"}
        entries: list[JsonObject] = []
        for device_id in sorted(self._records.keys() | live.keys()):
            record = self._records.get(device_id)
            conn = live.get(device_id)
            if conn is not None:
                manifest, last_seen = conn.manifest, conn.last_seen
            else:
                assert record is not None
                manifest, last_seen = record.manifest, record.last_seen
            entries.append(
                {
                    "device_id": device_id,
                    "name": None if record is None else record.name,
                    "pending": conn is not None and conn.state in ("pending", "pairing"),
                    "online": conn is not None,
                    "last_seen": last_seen,
                    "board": manifest.board,
                    "firmware": manifest.firmware,
                }
            )
        return entries

    def paired(self) -> list[JsonObject]:
        """Paired devices as agents see them, by name: pending ones are hidden."""
        return [
            {
                "name": record.name,
                "online": (conn := self._live.get(record.device_id)) is not None
                and conn.state == "online",
                "manifest": record.manifest.to_json(),
            }
            for record in sorted(self._records.values(), key=lambda r: r.name)
        ]

    async def approve(self, device: str, name: str | None = None) -> JsonObject:
        """Pair a pending device and welcome it with its new token."""
        pending = [d for d, c in self._live.items() if c.state == "pending"]
        device_id = self._find(device, pending)
        if name is not None:
            self._check_name(name, device_id)
        record = await self._pair(self._live[device_id], name)
        return {"device_id": record.device_id, "name": record.name}

    async def rename(self, device: str, name: str) -> JsonObject:
        """Rename a paired device. Its Connection drops, and it reconnects under the new name."""
        record = self._record(device)
        self._check_name(name, record.device_id)
        if name != record.name:
            old_name, record.name = record.name, name
            if (conn := self._live.pop(record.device_id, None)) is not None:
                conn.close("renamed")
            try:
                await self._save()
            except BaseException:
                record.name = old_name
                raise
        return {"device_id": record.device_id, "name": record.name}

    async def forget(self, device: str) -> None:
        """Remove a paired device. It must pair again."""
        record = self._record(device)
        del self._records[record.device_id]
        if (conn := self._live.pop(record.device_id, None)) is not None:
            conn.close("forgotten")
        try:
            await self._save()
        except BaseException:
            self._records.setdefault(record.device_id, record)
            raise

    async def identify(self, device: str, seconds: int | None = None) -> JsonObject:
        """Blink a paired device white for `seconds`, or the device's default. Raises
        `HubError`."""
        conn = self._online(device)
        args = {} if seconds is None else {"s": seconds}
        check_args(DEVICE_COMMANDS["identify"], args)
        await conn.command(None, "identify", args)
        return {"device_id": conn.manifest.device_id, "name": conn.name}

    async def diag(self, device: str) -> JsonObject:
        """A paired device's health, as it reports it. Raises `HubError`."""
        return await self._online(device).command(None, "diag", {})

    async def set_volume(self, device: str, level: int) -> JsonObject:
        """Store a paired device's volume, and set it on the device if it's online. The
        device gets it again first thing after every `welcome`. Raises `HubError`."""
        record = self._record(device)
        commands = volume_commands(record.manifest, level)
        if not commands:
            raise HubError("unsupported", f"{record.name} has no speaker")
        check_args(KINDS["speaker"].commands["set_volume"], {"level": level})
        old, record.volume = record.volume, level
        try:
            await self._save()
        except BaseException:
            if record.volume == level:
                record.volume = old
            raise
        applied = False
        conn = self._live.get(record.device_id)
        if conn is not None and conn.state == "online":
            try:
                await conn.pipeline(commands)
            except HubError as e:
                if e.code != "disconnected":
                    message = (
                        f"{e.message}; the volume is stored, for when {record.name} reconnects"
                    )
                    raise HubError(e.code, message) from None
            else:
                applied = True
        return {"device_id": record.device_id, "name": record.name, "applied": applied}

    # Agent Addresses.

    def named(self, name: str) -> DeviceRecord:
        """The paired device of that name. Raises `HubError`: `unknown_address`."""
        record = next((r for r in self._records.values() if r.name == name), None)
        if record is None:
            raise HubError("unknown_address", f"no paired device {name!r}")
        return record

    def resolve(self, address: str) -> tuple[DeviceRecord, Capability]:
        """The capability at an Address: `<device>/<capability id>`, or a bare capability id
        that only one paired device has. It goes by each device's last Manifest.

        Raises `HubError`: `unknown_address`, or `invalid_args` for a bare id that several
        devices have, with the candidates in `candidates`.
        """
        device, slash, cap_id = address.rpartition("/")
        if slash:
            records = [self.named(device)]
        else:
            records = sorted(self._records.values(), key=lambda r: r.name)
        matches = [(r, c) for r in records for c in r.manifest.capabilities if c.id == cap_id]
        if not matches:
            raise HubError("unknown_address", f"no capability at {address!r}")
        if len(matches) > 1:
            candidates = [f"{r.name}/{c.id}" for r, c in matches]
            raise HubError(
                "invalid_args",
                f"{address!r} is ambiguous: {', '.join(candidates)}",
                candidates=candidates,
            )
        return matches[0]

    # The handshake, called by DeviceConnection.

    def hello(self, conn: DeviceConnection) -> None:
        assert conn.hello is not None
        device_id = conn.manifest.device_id
        record = self._records.get(device_id)
        token = conn.hello.token
        if token is not None and (record is None or _sha256(token) != record.token_sha256):
            conn.send(Error("unauthorized", "the token doesn't match", PROTOCOL))
            conn.close("unauthorized")
            return
        # A refused hello never gets this far, so it can't knock the device off.
        if (old := self._live.get(device_id)) is not None:
            old.close("superseded")
        self._live[device_id] = conn
        if record is not None and token is not None:
            self._welcome(conn, record)
        elif self._auto_approve:
            conn.state = "pairing"
            self._spawn(self._pair(conn, None))
        else:
            conn.state = "pending"
            conn.send(Pending())
            _log.info("%s: pending, board %s", conn, conn.manifest.board)

    def detach(self, conn: DeviceConnection) -> None:
        self._connections.discard(conn)
        if conn.hello is None:
            return
        device_id = conn.manifest.device_id
        if self._live.get(device_id) is conn:
            del self._live[device_id]
        if not conn.conn_id:
            return  # never welcomed
        if (record := self._records.get(device_id)) is not None:
            record.last_seen = conn.last_seen
            self._spawn(self._save())
        self._presence.device_offline(conn)

    # Internals.

    async def _pair(self, conn: DeviceConnection, name: str | None) -> DeviceRecord:
        """Issue a new token to `conn`'s device, then welcome it. `name` is checked already."""
        conn.state = "pairing"
        manifest = conn.manifest
        old = self._records.get(manifest.device_id)
        if name is None:
            name = old.name if old is not None else self._default_name(manifest)
        token = secrets.token_urlsafe(32)
        volume = None if old is None else old.volume
        record = DeviceRecord(manifest.device_id, name, _sha256(token), manifest, _now_ms(), volume)
        self._records[record.device_id] = record
        try:
            await self._save()
        except BaseException:
            if self._records.get(record.device_id) is record:
                if old is None:
                    del self._records[record.device_id]
                else:
                    self._records[record.device_id] = old
            if conn.state == "pairing":
                conn.state = "pending"  # the owner can approve it again
            raise
        if conn.state == "pairing":
            self._welcome(conn, record, token)
        return record

    def _welcome(
        self, conn: DeviceConnection, record: DeviceRecord, token: str | None = None
    ) -> None:
        used = {c.conn_id for c in self._live.values()}
        while (conn_id := secrets.randbits(32)) == 0 or conn_id in used:
            pass
        conn.conn_id = conn_id
        conn.name = record.name
        conn.state = "online"
        conn.send(Welcome(record.name, conn_id, self._udp_port, token))
        record.manifest = conn.manifest
        record.last_seen = _now_ms()
        self._spawn(self._save())
        _log.info("%s: online as %s", conn, record.name)
        self._presence.device_online(conn, record)

    def _default_name(self, manifest: Manifest) -> str:
        """`<board-short>-<last 4 hex of the MAC>`, e.g. `echo-c3d4`, made unique."""
        board = re.sub("[^a-z0-9]+", "-", manifest.board.lower()).strip("-")
        base = f"{board.rsplit('-', 1)[-1] or 'device'}-{manifest.device_id[-4:]}"
        name, n = base, 2
        while self._name_owner(name) not in (None, manifest.device_id):
            name, n = f"{base}-{n}", n + 1
        return name

    def _check_name(self, name: str, device_id: str) -> None:
        if not NAME.fullmatch(name):
            raise HubError("invalid_name", f"{name!r}: a name must match [a-z0-9-]+")
        if self._name_owner(name) not in (None, device_id):
            raise HubError("name_taken", f"another device is named {name!r}")

    def _record(self, device: str) -> DeviceRecord:
        """A paired device, by its name, its device_id or a unique prefix of it. Raises
        `HubError`: `not_found` or `ambiguous`."""
        return self._records[self._find(device, list(self._records))]

    def _online(self, device: str) -> DeviceConnection:
        """A paired device's online Connection. Raises `HubError`: `not_found`, `ambiguous`
        or `disconnected`."""
        record = self._record(device)
        conn = self._live.get(record.device_id)
        if conn is None or conn.state != "online":
            raise HubError("disconnected", f"{record.name} is offline")
        return conn

    def _name_owner(self, name: str) -> str | None:
        return next((r.device_id for r in self._records.values() if r.name == name), None)

    def _find(self, query: str, device_ids: list[str]) -> str:
        """Resolve a name, a device_id or a unique device_id prefix among `device_ids`."""
        if (owner := self._name_owner(query)) is not None and owner in device_ids:
            return owner
        matches = [d for d in device_ids if query and d.startswith(query)]
        if not matches:
            raise HubError("not_found", f"no such device: {query!r}")
        if len(matches) > 1:
            raise HubError("ambiguous", f"{query!r} matches {', '.join(sorted(matches))}")
        return matches[0]

    async def _save(self) -> None:
        await self._store.save(list(self._records.values()))

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (e := task.exception()) is not None:
            _log.error("background task failed", exc_info=e)


def volume_commands(manifest: Manifest, level: int) -> list[CommandSpec]:
    """The commands that set the volume: `set_volume` on each speaker, as `(cap, name, args)`."""
    return [
        (cap.id, "set_volume", {"level": level})
        for cap in manifest.capabilities
        if cap.kind == "speaker"
    ]


def check_args(contract: CommandContract, args: JsonObject) -> None:
    """Raises `HubError`: `invalid_args` if `args` break the command's contract."""
    try:
        validate(contract.args, args, "args")
    except SchemaError as e:
        raise HubError("invalid_args", str(e)) from None


def _resolve(future: asyncio.Future[JsonObject], result: JsonObject) -> None:
    if not future.done():  # its waiter may have gone
        future.set_result(result)


def _fail(future: asyncio.Future[JsonObject], error: HubError) -> None:
    if not future.done():
        future.set_exception(error)


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _now_us() -> int:
    """The hub's monotonic clock, as `ping.t` carries it."""
    return time.monotonic_ns() // 1000
