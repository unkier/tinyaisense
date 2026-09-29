"""The per-device controllers: agent ops on devices, and keeping each device up to date with
the Session.

Each online device has a controller. Agent ops on it run first in, first out, and each awaits
its replies before the next starts; within an op, commands pipeline. Devices never block each
other, and device Events apply at once, outside the queue.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any, Literal, Protocol

from tinyaisense_wire import KINDS, Capability, JsonObject

from .devices import CommandSpec, DeviceConnection, check_args, volume_commands
from .errors import HubError
from .state import DeviceRecord

type _Op[T] = Callable[[], Coroutine[Any, Any, T]]
type _HubStatus = Literal["no_agent", "agent"]

_log = logging.getLogger("tinyaisense_hub.control")


class Session(Protocol):
    """A Session, as the controllers see it: where its agent's notifications go."""

    def notify(self, method: str, params: JsonObject) -> None: ...


class Controller:
    """One online Connection's controller: its ops run one at a time, in order."""

    def __init__(self, conn: DeviceConnection, workers: set[asyncio.Task[None]]) -> None:
        """`workers` collects the task that runs the ops, while it runs."""
        self.conn = conn
        self._queue: deque[tuple[_Op[Any], asyncio.Future[Any]]] = deque()
        self._worker: asyncio.Task[None] | None = None
        self._workers = workers

    def submit[T](self, op: _Op[T]) -> asyncio.Future[T]:
        """Queue `op`. The future holds its outcome. Cancelling the future before the op starts
        takes it off the queue; once it has started, it runs to the end."""
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        self._queue.append((op, future))
        if self._worker is None:
            self._worker = asyncio.create_task(self._work())
            self._workers.add(self._worker)
            self._worker.add_done_callback(self._workers.discard)
        return future

    async def _work(self) -> None:
        while self._queue:
            op, future = self._queue.popleft()
            if future.done():
                continue  # cancelled while it waited
            try:
                result = await op()
            except Exception as e:
                if not future.done():
                    future.set_exception(e)
            else:
                if not future.done():
                    future.set_result(result)
        self._worker = None


class LedClaim:
    """A Session's claim on one device's LEDs: what brings each led capability back."""

    def __init__(self) -> None:
        self._commands: dict[str, dict[int | None, CommandSpec]] = {}
        """Capability id → its commands to replay, by LED index; None for all of them."""

    def record(self, cap: str, name: str, args: JsonObject) -> None:
        commands = self._commands.setdefault(cap, {})
        index: int | None = args.get("index")
        if index is None:
            commands.clear()  # `off`, or a `set` of every LED, covers what came before
        else:
            commands.pop(index, None)  # replayed in order: after the latest whole-LED command
        commands[index] = (cap, name, args)

    def replay(self) -> list[CommandSpec]:
        return [command for commands in self._commands.values() for command in commands.values()]

    def releases(self) -> list[CommandSpec]:
        """The commands that end the claim."""
        return [(cap, "release", {}) for cap in self._commands]


class Control:
    """The controllers of the online devices, and what the Session wants of each device."""

    def __init__(self) -> None:
        self._controllers: dict[str, Controller] = {}
        """device_id → the controller of its online Connection."""
        self._workers: set[asyncio.Task[None]] = set()
        self._session: Session | None = None
        self._claims: dict[str, LedClaim] = {}
        """device_id → the Session's LED claim on it."""

    async def close(self) -> None:
        """Wait for every op to finish: they fail fast once the Connections are closed."""
        while self._workers:
            await asyncio.wait(self._workers)

    # Presence, from the registry.

    def device_online(self, conn: DeviceConnection, record: DeviceRecord) -> None:
        """Bring the device up to date, then tell the Session it's online."""
        controller = Controller(conn, self._workers)
        self._controllers[conn.manifest.device_id] = controller
        _log_failure(controller.submit(lambda: self._bring_up(conn, record)))

    def device_offline(self, conn: DeviceConnection) -> None:
        device_id = conn.manifest.device_id
        if (controller := self._controllers.get(device_id)) is not None and controller.conn is conn:
            del self._controllers[device_id]
        if self._session is not None:
            # By its welcome name: a rename or forget has changed the record already.
            self._session.notify("device.offline", {"device": conn.name})

    async def _bring_up(self, conn: DeviceConnection, record: DeviceRecord) -> None:
        """After `welcome`, pipelined: the owner's volume, the Status, then the LED claim."""
        commands: list[CommandSpec] = []
        if record.volume is not None:
            # Read now, not at `welcome`: the owner may have changed it since.
            commands += volume_commands(conn.manifest, record.volume)
        commands.append(_status(self._hub_status()))
        if (claim := self._claims.get(conn.manifest.device_id)) is not None:
            commands += claim.replay()
        await conn.pipeline(commands, strict=False)
        if conn.state == "online" and self._session is not None:
            self._session.notify(
                "device.online", {"device": conn.name, "manifest": conn.manifest.to_json()}
            )

    # The Session, from the Session manager.

    def session_started(self, session: Session) -> None:
        self._session = session
        self._claims = {}
        for controller in self._controllers.values():
            _log_failure(controller.submit(_pipeline_op(controller.conn, [_status("agent")])))

    async def session_ended(self, session: Session) -> None:
        """Release the Session's LED claims, then set every device's Status to `no_agent`."""
        self._session = None
        claims, self._claims = self._claims, {}
        ops: list[asyncio.Future[None]] = []
        for device_id, controller in self._controllers.items():
            ops.append(controller.submit(_end_op(controller.conn, claims.get(device_id))))
        await asyncio.gather(*ops, return_exceptions=True)

    # Agent ops, from the core.

    async def command(
        self,
        session: Session,
        device: DeviceRecord,
        capability: Capability,
        name: str,
        args: JsonObject,
    ) -> JsonObject:
        """Send an agent's Kind command. Raises `HubError`."""
        address = f"{device.name}/{capability.id}"
        kind = KINDS.get(capability.kind)
        if kind is None:
            raise HubError("unsupported", f"{address} is of a Kind the hub doesn't know")
        contract = kind.commands.get(name)
        if contract is None or contract.hub_only:
            raise HubError("unsupported", f"{address} has no command {name!r}")
        check_args(contract, args)
        controller = self._controller(device)

        async def op() -> JsonObject:
            self._require_current(session)
            claim = None
            if capability.kind == "led":
                claim = self._claims.setdefault(device.device_id, LedClaim())
            result = await controller.conn.command(capability.id, name, args)
            if claim is not None:
                # Only once the device did it: a failed command isn't replayed on reconnect.
                claim.record(capability.id, name, args)
            return result

        return await controller.submit(op)

    async def led_release(self, session: Session, device: DeviceRecord) -> None:
        """Hand the device's LEDs back to its Status. Raises `HubError`."""
        self._require_current(session)
        controller = self._controllers.get(device.device_id)
        if controller is None:
            self._claims.pop(device.device_id, None)  # its claim ended with its Connection
            return

        async def op() -> None:
            self._require_current(session)
            if (claim := self._claims.pop(device.device_id, None)) is not None:
                await controller.conn.pipeline(claim.releases())

        await controller.submit(op)

    # Internals.

    def _hub_status(self) -> _HubStatus:
        return "no_agent" if self._session is None else "agent"

    def _controller(self, device: DeviceRecord) -> Controller:
        """Raises `disconnected` if the device is offline."""
        controller = self._controllers.get(device.device_id)
        if controller is None:
            raise HubError("disconnected", f"{device.name} is offline")
        return controller

    def _require_current(self, session: Session) -> None:
        if self._session is not session:
            raise HubError("disconnected", "the Session has ended")


def _status(state: _HubStatus) -> CommandSpec:
    return (None, "status", {"state": state})


def _pipeline_op(conn: DeviceConnection, commands: list[CommandSpec]) -> _Op[None]:
    return lambda: conn.pipeline(commands)


def _end_op(conn: DeviceConnection, claim: LedClaim | None) -> _Op[None]:
    """Release `claim`, then set the Status to `no_agent`. The claim is read when the op
    starts, so it holds what the ops queued before it recorded."""

    async def op() -> None:
        releases = claim.releases() if claim is not None else []
        await conn.pipeline([*releases, _status("no_agent")])

    return op


def _log_failure(future: asyncio.Future[Any]) -> None:
    """An op nobody awaits: log how it failed."""

    def done(future: asyncio.Future[Any]) -> None:
        if not future.cancelled() and (e := future.exception()) is not None:
            _log.warning("device op failed: %s", e)

    future.add_done_callback(done)
