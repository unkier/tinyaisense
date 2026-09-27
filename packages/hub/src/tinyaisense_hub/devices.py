"""Device Connections, the handshake and Pairing."""

import asyncio
import hashlib
import logging
import re
import secrets
import socket
import time
from collections.abc import Coroutine
from typing import Any, Literal, cast

from tinyaisense_wire import (
    PROTOCOL,
    ControlDecoder,
    Error,
    Hello,
    JsonObject,
    Manifest,
    Message,
    Pending,
    Ping,
    Pong,
    Welcome,
    WireError,
    encode_control,
)

from .config import Timing
from .errors import HubError
from .keepalive import ClockSync, Dead, Inbound, Keepalive, SendPing, Tick
from .state import DeviceRecord, StateStore

NAME = re.compile("[a-z0-9-]+")
"""Device names."""

_log = logging.getLogger("tinyaisense_hub.devices")

ConnectionState = Literal["hello", "pending", "pairing", "online", "closed"]


class DeviceConnection(asyncio.Protocol):
    """One device's Connection, from TCP accept to drop. Every handler is synchronous."""

    def __init__(self, registry: Registry, timing: Timing) -> None:
        self.state: ConnectionState = "hello"
        self.hello: Hello | None = None
        self.conn_id = 0
        """Set by `welcome`."""
        self.clock = ClockSync(window=timing.clock_window_ms * 1000)
        """The device clock, from its pongs."""
        self._registry = registry
        self._timing = timing
        self._decoder = ControlDecoder()
        self._transport: asyncio.Transport | None = None
        self._hello_timer: asyncio.TimerHandle | None = None
        self._keepalive: Keepalive | None = None
        """Set by `hello`."""
        self._keepalive_timer: asyncio.TimerHandle | None = None
        self._keepalive_timer_at = 0
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
            case _:
                pass

    def _run_keepalive(self, now: int) -> None:
        """Do what the keepalive has due, and set the timer for its next deadline."""
        assert self._keepalive is not None
        for action in self._keepalive.handle(Tick(), now):
            match action:
                case SendPing(t):
                    self.send(Ping(t))
                case Dead():
                    self.abort(f"dead: no frame for {self._timing.dead_after_ms} ms")
        deadline = self._keepalive.next_deadline()
        if self.state == "closed" or deadline is None:
            return
        # Inbound frames only push the deadline later, so a timer that fires early just
        # finds nothing due and comes back here.
        if self._keepalive_timer is None or deadline < self._keepalive_timer_at:
            self._stop_timer(self._keepalive_timer)
            self._keepalive_timer_at = deadline
            self._keepalive_timer = asyncio.get_running_loop().call_later(
                max(0, deadline - now) / 1_000_000, self._keepalive_due
            )

    def _keepalive_due(self) -> None:
        self._keepalive_timer = None
        self._run_keepalive(_now_us())

    def _teardown(self, reason: str) -> bool:
        """Release everything the Connection holds, once. Returns whether this call did."""
        if self.state == "closed":
            return False
        _log.info("%s: Connection closed: %s", self, reason)
        self.state = "closed"
        self._stop_timer(self._hello_timer)
        self._stop_timer(self._keepalive_timer)
        self._hello_timer = self._keepalive_timer = None
        self._registry.detach(self)
        return True

    @staticmethod
    def _stop_timer(timer: asyncio.TimerHandle | None) -> None:
        if timer is not None:
            timer.cancel()


class Registry:
    """Paired devices, pending devices and their live Connections. Owns Pairing.

    A device is pending while it has an open Connection without a token. Pending devices
    live in memory only.
    """

    def __init__(
        self, store: StateStore, *, udp_port: int, timing: Timing, auto_approve: bool
    ) -> None:
        self._store = store
        self._records = store.load()
        self._live: dict[str, DeviceConnection] = {}
        """device_id → its pending or online Connection."""
        self._udp_port = udp_port
        self._timing = timing
        self._auto_approve = auto_approve
        self._tasks: set[asyncio.Task[Any]] = set()

    def connection(self) -> DeviceConnection:
        """The protocol factory for the device TCP server."""
        return DeviceConnection(self, self._timing)

    async def close(self) -> None:
        """Wait for background saves. Close the Connections first."""
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
        record = self._records[self._find(device, list(self._records))]
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
        device_id = self._find(device, list(self._records))
        record = self._records.pop(device_id)
        if (conn := self._live.pop(device_id, None)) is not None:
            conn.close("forgotten")
        try:
            await self._save()
        except BaseException:
            self._records.setdefault(device_id, record)
            raise

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
        if conn.hello is None:
            return
        device_id = conn.manifest.device_id
        if self._live.get(device_id) is conn:
            del self._live[device_id]
        if conn.conn_id and (record := self._records.get(device_id)) is not None:
            record.last_seen = conn.last_seen
            self._spawn(self._save())

    # Internals.

    async def _pair(self, conn: DeviceConnection, name: str | None) -> DeviceRecord:
        """Issue a new token to `conn`'s device, then welcome it. `name` is checked already."""
        conn.state = "pairing"
        manifest = conn.manifest
        old = self._records.get(manifest.device_id)
        if name is None:
            name = old.name if old is not None else self._default_name(manifest)
        token = secrets.token_urlsafe(32)
        record = DeviceRecord(manifest.device_id, name, _sha256(token), manifest, _now_ms())
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
        conn.state = "online"
        conn.send(Welcome(record.name, conn_id, self._udp_port, token))
        record.manifest = conn.manifest
        record.last_seen = _now_ms()
        self._spawn(self._save())
        _log.info("%s: online as %s", conn, record.name)

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


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _now_us() -> int:
    """The hub's monotonic clock, as `ping.t` carries it."""
    return time.monotonic_ns() // 1000
