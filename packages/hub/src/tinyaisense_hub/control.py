"""The per-device controllers: agent ops on devices, and keeping each device up to date with
the Session.

Each online device has a controller. Agent ops on it run first in, first out, and each awaits
its replies before the next starts; within an op, commands pipeline. Devices never block each
other, and device Events apply at once, outside the queue.
"""

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any, Literal

from tinyaisense_wire import KINDS, Capability, Event, JsonObject, Manifest

from .alarm import now_us
from .config import Timing
from .devices import CommandSpec, DeviceConnection, check_args, volume_commands
from .errors import HubError
from .mic import MicStream
from .session import Session
from .speaker import DRAINED, STOP_RESULT, STOPPED, Speakers, SpeakerStream, stats
from .state import DeviceRecord
from .streams import FORMAT, EndReason, Stream, StreamEnd

type _Op[T] = Callable[[], Coroutine[Any, Any, T]]
type _HubStatus = Literal["no_agent", "agent"]

_PREFILL_MS = 40
"""A speaker stream's default `prefill_ms`."""

_log = logging.getLogger("tinyaisense_hub.control")


class Controller:
    """One online Connection's controller: its ops run one at a time, in order."""

    def __init__(self, conn: DeviceConnection, workers: set[asyncio.Task[None]]) -> None:
        """`workers` collects the task that runs the ops, while it runs."""
        self.conn = conn
        self._queue: deque[tuple[_Op[Any], asyncio.Future[Any]]] = deque()
        self._worker: asyncio.Task[None] | None = None
        self._workers = workers
        self.streams: dict[str, Stream] = {}
        """Capability id → the agent stream open on it."""

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

    def __init__(self, timing: Timing) -> None:
        self._timing = timing
        self._speakers = Speakers(timing, self._drain)
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
        self._speakers.close()

    # The devices, from the registry.

    def device_online(self, conn: DeviceConnection, record: DeviceRecord) -> None:
        """Bring the device up to date, then tell the Session it's online."""
        controller = Controller(conn, self._workers)
        self._controllers[conn.manifest.device_id] = controller
        _log_failure(controller.submit(lambda: self._bring_up(conn, record)))

    def device_event(self, conn: DeviceConnection, event: Event) -> None:
        """Apply a device Event at once, outside the op queue."""
        controller = self._controllers.get(conn.manifest.device_id)
        if controller is None or controller.conn is not conn:
            return
        stream = controller.streams.get(event.cap)
        if not isinstance(stream, SpeakerStream) or event.data.get("stream") != stream.stream_id:
            return
        if event.name == "drained":
            self._end(stream, "drained", stats(DRAINED, event.data))
        elif event.name == "stopped":  # the Reflex cut the speaker
            self._end(stream, "reflex", stats(STOPPED, event.data))

    def device_offline(self, conn: DeviceConnection) -> None:
        device_id = conn.manifest.device_id
        if (controller := self._controllers.get(device_id)) is not None and controller.conn is conn:
            for stream in list(controller.streams.values()):
                self._end(stream, "disconnected", None)
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
        """Stop the Session's streams, release its LED claims, then set every device's
        Status to `no_agent`."""
        self._session = None
        claims, self._claims = self._claims, {}
        ops: list[asyncio.Future[None]] = []
        for device_id, controller in self._controllers.items():
            ops.append(controller.submit(self._end_op(controller, session, claims.get(device_id))))
        await asyncio.gather(*ops, return_exceptions=True)

    def _end_op(
        self, controller: Controller, session: Session, claim: LedClaim | None
    ) -> _Op[None]:
        """Stop the Session's streams, release `claim`, then set the Status to `no_agent`,
        pipelined. The streams and the claim are read when the op starts, so they hold what
        the ops queued before it did."""

        async def op() -> None:
            conn = controller.conn
            streams = [s for s in controller.streams.values() if s.session is session]
            commands = [self._halt(stream) for stream in streams]
            commands += claim.releases() if claim is not None else []
            commands.append(_status("no_agent"))
            replies = await asyncio.gather(
                *(conn.command(*command) for command in commands), return_exceptions=True
            )
            await self._finish_all(streams, "disconnected", replies)
            if failures := [r for r in replies[len(streams) :] if isinstance(r, BaseException)]:
                raise failures[0]

        return op

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

    async def stream_start(
        self,
        session: Session,
        device: DeviceRecord,
        capability: Capability,
        prefill_ms: int | None,
    ) -> int:
        """Open a stream for the agent: to a speaker, or from a mic. A stream already open on
        the capability, or on its peers in an `exclusive` group, ends `preempted`. Returns the
        handle. Raises `HubError`."""
        address = f"{device.name}/{capability.id}"
        if capability.kind not in ("speaker", "mic"):
            raise HubError("unsupported", f"{address} takes no stream")
        if not FORMAT.offered(capability.params):
            raise HubError("unsupported", f"{address} has no 16 kHz mono PCM in 10 ms frames")
        if capability.kind == "mic" and prefill_ms is not None:
            raise HubError("invalid_args", "prefill_ms is for speaker streams")
        controller = self._controller(device)

        async def op() -> int:
            self._require_current(session)
            conn = controller.conn
            stream_id = conn.stream_ids.open()
            handle = session.new_handle()
            args: JsonObject = {"stream": stream_id, "format": FORMAT.to_json()}
            stream: Stream
            if capability.kind == "speaker":
                args["prefill_ms"] = _PREFILL_MS if prefill_ms is None else prefill_ms
                stream = SpeakerStream(handle, session, conn, capability.id, stream_id, address)
            else:
                tail = self._timing.mic_tail_ms * 1000
                stream = MicStream(
                    handle, session, conn, capability.id, stream_id, address, tail=tail
                )
                conn.receivers[stream_id] = stream  # its first frames may beat the reply
            preempted = [
                old
                for cap in _group(conn.manifest, capability.id)
                if (old := controller.streams.get(cap)) is not None
            ]
            commands = [self._halt(old) for old in preempted]
            commands.append((capability.id, "start", args))
            replies = await asyncio.gather(
                *(conn.command(*command) for command in commands), return_exceptions=True
            )
            await self._finish_all(preempted, "preempted", replies)
            started = replies[-1]
            if self._controllers.get(device.device_id) is not controller:
                started = HubError("disconnected", f"{device.name} went offline")
            if isinstance(started, BaseException):
                if isinstance(started, HubError) and started.code == "timeout":
                    # The device may yet start it: the capability mustn't stay busy with it.
                    with contextlib.suppress(HubError):
                        await conn.command(capability.id, "stop", {})
                if conn.receivers.get(stream_id) is stream:
                    del conn.receivers[stream_id]
                conn.stream_ids.close(stream_id)
                raise started
            controller.streams[capability.id] = stream
            session.streams[handle] = stream
            session.notify(
                "stream.started", {"handle": handle, "address": address, "rate": FORMAT.rate}
            )
            if isinstance(stream, SpeakerStream):
                self._speakers.open(stream)
            else:
                stream.started()
            return handle

        return await controller.submit(op)

    def stream_feed(self, session: Session, handle: int, pcm: bytes) -> None:
        """Take the agent's audio for a stream. Audio for a stream that isn't open is
        dropped."""
        stream = session.streams.get(handle)
        if not isinstance(stream, SpeakerStream) or not stream.open:
            return
        if self._speakers.feed(stream, pcm):
            self._halt(stream)
            self._submit_stop(stream, "overflow")

    async def stream_stop(self, session: Session, handle: int) -> StreamEnd:
        """End a stream at once. Raises `HubError`: `disconnected` if it has ended."""
        stream = self._stream(session, handle)
        if stream.open:
            # Its audio stops now, even while the `stop` waits its turn on the device.
            self._halt(stream)
            self._submit_stop(stream, "stopped")
        return await asyncio.shield(stream.ended)

    async def stream_drain(self, session: Session, handle: int) -> StreamEnd:
        """End a speaker stream once what the agent sent has played. Raises `HubError`:
        `disconnected` if it has ended, `unsupported` for a mic stream."""
        stream = self._stream(session, handle)
        if not isinstance(stream, SpeakerStream):
            raise HubError("unsupported", f"stream {handle} is a mic stream: stop it instead")
        if stream.open:
            self._speakers.drain(stream)
        return await asyncio.shield(stream.ended)

    # Internals.

    def _stream(self, session: Session, handle: int) -> Stream:
        stream = session.streams.get(handle)
        if stream is None:
            raise HubError("disconnected", f"stream {handle} has ended")
        return stream

    def _halt(self, stream: Stream) -> CommandSpec:
        """Mark the stream stopping, and stop sending a speaker stream's audio. Returns the
        command that stops the stream on its device; once that's replied to, `_finish` it."""
        stream.stopping = True
        if isinstance(stream, SpeakerStream):
            self._speakers.remove(stream)
        return (stream.cap, "stop", {})

    def _submit_stop(self, stream: Stream, reason: EndReason) -> None:
        """Queue an op that stops the stream on its device, and ends it with `reason`. If the
        device has dropped, it has ended already."""
        controller = self._controllers.get(stream.conn.manifest.device_id)
        if controller is not None and controller.conn is stream.conn:
            _log_failure(controller.submit(lambda: self._stop(stream, reason)))

    async def _stop(self, stream: Stream, reason: EndReason) -> None:
        if stream.ended.done():
            return
        try:
            reply = await stream.conn.command(*self._halt(stream))
        except HubError as e:
            reply = e
        await self._finish(stream, reason, reply)

    async def _finish(
        self, stream: Stream, reason: EndReason, reply: JsonObject | BaseException
    ) -> None:
        """End a stream with `reason`, given its device's reply to `stop`. A mic stream first
        waits for its tail frames."""
        if isinstance(stream, MicStream):
            stream.stopped(reply)
            await stream.finished
            self._end(stream, reason, None)
        else:
            self._end(stream, reason, _stop_stats(reply))

    async def _finish_all(
        self,
        streams: list[Stream],
        reason: EndReason,
        replies: list[JsonObject | BaseException],
    ) -> None:
        """`_finish` each stream at once, given the replies to their `stop`s, in order."""
        await asyncio.gather(
            *(
                self._finish(stream, reason, reply)
                for stream, reply in zip(streams, replies, strict=False)
            )
        )

    def _drain(self, stream: SpeakerStream, last_seq: int | None) -> None:
        """The pacer has sent a drained stream's last frame: tell the device at once, outside
        the queue. If it fails the `drain`, no `drained` will come: stop the stream."""

        def sent(drain: asyncio.Future[JsonObject]) -> None:
            if drain.cancelled() or (e := drain.exception()) is None or not stream.open:
                return
            _log.warning("%s: drain failed, so the stream stops: %s", stream.address, e)
            self._halt(stream)
            self._submit_stop(stream, "stopped")

        stream.conn.command(stream.cap, "drain", {"last_seq": last_seq}).add_done_callback(sent)

    def _end(self, stream: Stream, reason: EndReason, stats: JsonObject | None) -> None:
        """End a stream, once. With no `stats` from the device, a speaker stream's are the
        hub's estimate. A mic stream takes no more frames, and its stats are the hub's."""
        if stream.ended.done():
            return
        conn = stream.conn
        if isinstance(stream, SpeakerStream):
            self._speakers.remove(stream)
            if stats is None:
                stats = stream.estimate(now_us())
        elif isinstance(stream, MicStream):
            stream.close()
            if conn.receivers.get(stream.stream_id) is stream:
                del conn.receivers[stream.stream_id]
            # After the frames the agent's link still holds, which it no longer skips.
            stats = stream.stats(skipped=stream.session.audio_ended(stream.handle))
        assert stats is not None
        conn.stream_ids.close(stream.stream_id)
        controller = self._controllers.get(conn.manifest.device_id)
        if controller is not None and controller.streams.get(stream.cap) is stream:
            del controller.streams[stream.cap]
        if stream.session.streams.get(stream.handle) is stream:
            del stream.session.streams[stream.handle]
        end = StreamEnd(reason, stats)
        _log.info("%s: stream %d ended %s: %s", stream.address, stream.handle, reason, stats)
        stream.ended.set_result(end)
        stream.session.notify("stream.ended", {"handle": stream.handle, **end.to_json()})

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


def _group(manifest: Manifest, cap: str) -> list[str]:
    """`cap`, then the capabilities it shares an `exclusive` group with."""
    group = [cap]
    for exclusive in manifest.exclusive:
        if cap in exclusive:
            group += [peer for peer in exclusive if peer not in group]
    return group


def _status(state: _HubStatus) -> CommandSpec:
    return (None, "status", {"state": state})


def _pipeline_op(conn: DeviceConnection, commands: list[CommandSpec]) -> _Op[None]:
    return lambda: conn.pipeline(commands)


def _stop_stats(reply: JsonObject | BaseException) -> JsonObject | None:
    """The stats in a `stop` reply; None if it failed."""
    return None if isinstance(reply, BaseException) else stats(STOP_RESULT, reply)


def _log_failure(future: asyncio.Future[Any]) -> None:
    """An op nobody awaits: log how it failed."""

    def done(future: asyncio.Future[Any]) -> None:
        if not future.cancelled() and (e := future.exception()) is not None:
            _log.warning("device op failed: %s", e)

    future.add_done_callback(done)
