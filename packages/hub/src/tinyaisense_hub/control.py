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

from tinyaisense_wire import KINDS, Capability, Event, JsonObject, Manifest, SchemaError, validate

from .alarm import Alarm, now_us
from .config import Timing
from .devices import CommandSpec, DeviceConnection, check_args, volume_commands
from .errors import HubError
from .gestures import Down, Gesture, Gestures, Up
from .keepalive import Tick
from .mic import STARTED as MIC_STARTED
from .mic import Listener, MicStream
from .session import Session
from .speaker import DRAINED, STOP_RESULT, STOPPED, Speakers, SpeakerStream, stats
from .state import DeviceRecord
from .streams import FORMAT, EndReason, Stream, StreamEnd
from .thinking import Delivered, Moved, Thinking

type _Op[T] = Callable[[], Coroutine[Any, Any, T]]
type _HubStatus = Literal["no_agent", "agent", "thinking"]

_PREFILL_MS = 40
"""A speaker stream's default `prefill_ms`."""
_RATE = 16000
"""A stream's default rate, for the agent."""
_RELEASE_WAIT_MS = KINDS["mic"].commands["stop"].timeout_ms
"""How long a Reflex stream waits for its `mic.stopped` after the release, as for a `stop`."""

_log = logging.getLogger("tinyaisense_hub.control")


class Controller:
    """One online Connection's controller: its ops run one at a time, in order."""

    def __init__(
        self,
        conn: DeviceConnection,
        record: DeviceRecord,
        workers: set[asyncio.Task[None]],
        thinking: Thinking,
        think: Callable[[Controller, Tick, int], None],
        gestures: dict[str, Gestures],
        gesture: Callable[[Controller, Tick, int], None],
    ) -> None:
        """`workers` collects the task that runs the ops, while it runs. `think` runs the
        `thinking` core when its deadline is due, and `gesture` the `gestures` when one is."""
        self.conn = conn
        self.record = record
        """The device's record as it came online."""
        self.up_to_date = False
        """Whether the bring-up is done: the Session hears of the device from then on."""
        self.thinking = thinking
        """Whether the device shows `thinking`, for the Session."""
        self.thinking_alarm = Alarm(lambda now: think(self, Tick(), now))
        self.gestures = gestures
        """Button capability id → its gestures."""
        self.gestures_alarm = Alarm(lambda now: gesture(self, Tick(), now))
        self._queue: deque[tuple[_Op[Any], asyncio.Future[Any]]] = deque()
        self._worker: asyncio.Task[None] | None = None
        self._workers = workers
        self.streams: dict[str, Stream] = {}
        """Capability id → the Session's stream open on it: the agent's, the hub's for it, or
        the Reflex's."""

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
        self._disarmed: set[str] = set()
        """The device_ids whose Reflex the Session has disarmed. Every other one is armed while
        the Session lasts."""
        self.talk: Callable[[Session, DeviceRecord, Capability], Listener] | None = None
        """Makes the Utterance that push-to-talk on a mic is, and returns what hears it."""

    async def close(self) -> None:
        """Wait for every op to finish: they fail fast once the Connections are closed."""
        while self._workers:
            await asyncio.wait(self._workers)
        self._speakers.close()

    # The devices, from the registry.

    def device_online(self, conn: DeviceConnection, record: DeviceRecord) -> None:
        """Bring the device up to date, then tell the Session it's online."""
        timing = self._timing
        gestures = {
            cap.id: Gestures(
                click=timing.click_ms * 1000,
                double_click=timing.double_click_ms * 1000,
                long_press=timing.long_press_ms * 1000,
            )
            for cap in conn.manifest.capabilities
            if cap.kind == "button"
        }
        controller = Controller(
            conn, record, self._workers, self._thinking(), self._think, gestures, self._gestures_due
        )
        self._controllers[conn.manifest.device_id] = controller
        _log_failure(controller.submit(lambda: self._bring_up(conn, record)))

    def device_edge(self, conn: DeviceConnection, event: Event) -> None:
        """A raw button edge goes to the Session at once: the Interrupt path."""
        if (controller := self._current(conn)) is not None:
            at = conn.clock.to_hub(event.ts)
            self._button_event(controller, event.cap, event.name, event.data, at, edge=True)

    def device_event(self, conn: DeviceConnection, event: Event) -> None:
        """Apply a device Event at once, outside the op queue."""
        controller = self._current(conn)
        if controller is None:
            return
        if event.cap in controller.gestures and event.name in ("down", "up"):
            at = conn.clock.to_hub(event.ts)
            at = now_us() if at is None else at
            if event.name == "down":
                armed = self._armed(conn.manifest.device_id)
                self._gestures(controller, Down(at, armed), now_us(), event.cap)
                if armed:
                    self._hold(controller)
            else:
                gestures = self._gestures(controller, Up(at), now_us(), event.cap)
                self._release(controller, click=any(g.name == "click" for g in gestures))
            return
        stream = controller.streams.get(event.cap)
        ours = stream is not None and event.data.get("stream") == stream.stream_id
        if isinstance(stream, SpeakerStream) and ours:
            if event.name == "drained":
                self._end(stream, "drained", stats(DRAINED, event.data))
            elif event.name == "stopped":  # the Reflex cut the speaker
                self._end(stream, "reflex", stats(STOPPED, event.data))
        elif event.name == "started" and _kind(conn.manifest, event.cap) == "mic":
            self._reflex_started(controller, event)
        elif isinstance(stream, MicStream) and ours and stream.reflex and event.name == "stopped":
            if stream.open:  # no `up` closed it
                self._closing(stream)
            stream.stopped(event.data)

    def device_stream(self, conn: DeviceConnection, stream: int) -> MicStream | None:
        """A frame on a device-range id that isn't open: the Reflex's stream, if it's armed.
        Its frames wait for its `mic.started`."""
        controller = self._current(conn)
        return None if controller is None else self._reflex_open(controller, stream)

    def device_offline(self, conn: DeviceConnection) -> None:
        device_id = conn.manifest.device_id
        if (controller := self._current(conn)) is not None:
            for stream in list(controller.streams.values()):
                self._end(stream, "disconnected", None)
            controller.thinking_alarm.cancel()
            controller.gestures_alarm.cancel()
            del self._controllers[device_id]
        if self._session is not None:
            # By its welcome name: a rename or forget has changed the record already.
            self._session.notify("device.offline", {"device": conn.name})

    async def _bring_up(self, conn: DeviceConnection, record: DeviceRecord) -> None:
        """After `welcome`, pipelined: the owner's volume, the Status, the Reflex, then the LED
        claim. The device came up disarmed."""
        commands: list[CommandSpec] = []
        if record.volume is not None:
            # Read now, not at `welcome`: the owner may have changed it since.
            commands += volume_commands(conn.manifest, record.volume)
        commands.append(_status(self._hub_status()))
        if self._armed(conn.manifest.device_id):
            commands += _reflex(conn.manifest, armed=True)
        if (claim := self._claims.get(conn.manifest.device_id)) is not None:
            commands += claim.replay()
        await conn.pipeline(commands, strict=False)
        if (controller := self._current(conn)) is not None:
            controller.up_to_date = True
        if conn.state == "online" and self._session is not None:
            self._session.notify(
                "device.online", {"device": conn.name, "manifest": conn.manifest.to_json()}
            )

    # The Session, from the Session manager.

    def session_started(self, session: Session) -> None:
        self._session = session
        self._claims = {}
        self._disarmed = set()
        for controller in self._controllers.values():
            commands = [_status("agent"), *_reflex(controller.conn.manifest, armed=True)]
            _log_failure(controller.submit(_pipeline_op(controller.conn, commands)))

    async def session_ended(self, session: Session) -> None:
        """Stop the Session's speaker streams, then its mic streams, release its LED claims,
        disarm the Reflex, then set every device's Status to `no_agent`."""
        self._session = None
        claims, self._claims = self._claims, {}
        self._disarmed = set()
        ops: list[asyncio.Future[None]] = []
        for device_id, controller in self._controllers.items():
            controller.thinking = self._thinking()
            controller.thinking_alarm.cancel()
            ops.append(controller.submit(self._end_op(controller, session, claims.get(device_id))))
        await asyncio.gather(*ops, return_exceptions=True)

    def _end_op(
        self, controller: Controller, session: Session, claim: LedClaim | None
    ) -> _Op[None]:
        """Stop the Session's speaker streams, then its mic streams, which ends its listens,
        release `claim`, disarm the Reflex, then set the Status to `no_agent`, pipelined. The
        streams and the claim are read when the op starts, so they hold what the ops queued
        before it did."""

        async def op() -> None:
            conn = controller.conn
            streams = sorted(
                (s for s in controller.streams.values() if s.session is session),
                key=lambda stream: isinstance(stream, MicStream),
            )
            commands = [self._halt(stream) for stream in streams]
            commands += claim.releases() if claim is not None else []
            commands += _reflex(conn.manifest, armed=False)
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

    def _reflex_started(self, controller: Controller, event: Event) -> None:
        """The Reflex opened a mic stream: it starts, opening first if no frame has yet."""
        conn = controller.conn
        try:
            validate(MIC_STARTED, event.data, "data")
        except SchemaError as e:
            _log.warning("%s: bad mic.started: %s", conn.name, e)
            return
        stream_id: int = event.data["stream"]
        stream = conn.receivers.get(stream_id)
        if stream is None and stream_id not in conn.stream_ids.closed:
            stream = self._reflex_open(controller, stream_id)
        if isinstance(stream, MicStream) and stream.reflex and stream.cap == event.cap:
            stream.started()

    def _reflex_open(self, controller: Controller, stream_id: int) -> MicStream | None:
        """Open the stream the Reflex started on `stream_id`, while it's armed: push-to-talk,
        held. None if the hub takes no stream from it, and its frames are dropped. A stream
        the hub stopped on the mic, which waits for its tail, doesn't keep it out."""
        conn, record = controller.conn, controller.record
        session, mic = self._session, _reflex_mic(conn.manifest)
        if (
            session is None
            or self.talk is None
            or mic is None
            or ((old := controller.streams.get(mic.id)) is not None and old.open)
            or not self._armed(record.device_id)
        ):
            return None
        try:
            listener = self.talk(session, record, mic)
        except HubError as e:
            _log.warning("%s/%s: push-to-talk failed: %s", record.name, mic.id, e)
            return None
        tail = self._timing.mic_tail_ms * 1000
        address = f"{record.name}/{mic.id}"
        stream = MicStream.listened(session, conn, mic.id, stream_id, address, listener, tail=tail)
        stream.reflex = True
        conn.receivers[stream_id] = stream
        controller.streams[mic.id] = stream
        stream.held = True
        listener.held()
        self._think(controller, Moved())
        return stream

    def _hold(self, controller: Controller) -> None:
        """An armed press: a listen on the Reflex's mic is the human's to end, from now on."""
        mic = _reflex_mic(controller.conn.manifest)
        stream = None if mic is None else controller.streams.get(mic.id)
        if not isinstance(stream, MicStream) or stream.held or not stream.open:
            return
        if stream.listener is not None:
            stream.held = True
            stream.listener.held()

    def _release(self, controller: Controller, *, click: bool) -> None:
        """The button let go: the listen it held ends, `button`, or `click` after a click. The
        device closes a stream its Reflex opened by itself."""
        for stream in list(controller.streams.values()):
            if isinstance(stream, MicStream) and stream.held:
                stream.held = False
                if stream.reflex and stream.open:
                    self._closing(stream)
                if stream.listener is not None:
                    stream.listener.released(click)

    def _closing(self, stream: MicStream) -> None:
        """The device is closing a Reflex stream by itself: it ends `reflex` once its tail is
        in, after its `mic.stopped`, or once that's overdue."""
        stream.stopping = True
        task = asyncio.create_task(self._released(stream))
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)

    async def _released(self, stream: MicStream) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(_RELEASE_WAIT_MS / 1000 + self._timing.mic_tail_ms / 1000):
                await asyncio.shield(stream.finished)
        self._end(stream, "reflex", None)

    async def reflex_set(self, session: Session, device: DeviceRecord, armed: bool) -> None:
        """Arm or disarm the device's Reflex while the Session lasts, reconnects included.
        Raises `HubError`: `unsupported` for a device with no button."""
        self._require_current(session)
        if not _reflex(device.manifest, armed=armed):
            raise HubError("unsupported", f"{device.name} has no button")
        if armed:
            self._disarmed.discard(device.device_id)
        else:
            self._disarmed.add(device.device_id)
        controller = self._controllers.get(device.device_id)
        if controller is None:
            return  # it's armed or not as it comes back

        async def op() -> None:
            self._require_current(session)
            conn = controller.conn
            await conn.pipeline(_reflex(conn.manifest, armed=self._armed(device.device_id)))

        await controller.submit(op)

    async def stream_start(
        self,
        session: Session,
        device: DeviceRecord,
        capability: Capability,
        rate: int | None,
        prefill_ms: int | None,
    ) -> int:
        """Open a stream for the agent, at `rate` on its side: to a speaker, or from a mic. A
        stream already open on the capability, or on its peers in an `exclusive` group, ends
        `preempted`. Returns the handle. Raises `HubError`."""
        address = f"{device.name}/{capability.id}"
        if capability.kind not in ("speaker", "mic"):
            raise HubError("unsupported", f"{address} takes no stream")
        if not FORMAT.offered(capability.params):
            raise HubError("unsupported", f"{address} has no 16 kHz mono PCM in 10 ms frames")
        if capability.kind == "mic" and prefill_ms is not None:
            raise HubError("invalid_args", "prefill_ms is for speaker streams")
        rate = _RATE if rate is None else rate
        controller = self._controller(device)

        async def op() -> int:
            self._require_current(session)
            if capability.kind == "speaker":
                _check_held(controller, address)
            conn = controller.conn
            handle = session.new_handle()
            stream_id = conn.stream_ids.open()
            if capability.kind == "speaker":
                speaker = SpeakerStream(
                    handle, session, conn, capability.id, stream_id, address, rate
                )
                await self._open(controller, speaker, prefill_ms)
            else:
                tail = self._timing.mic_tail_ms * 1000
                mic = MicStream(
                    handle,
                    session,
                    conn,
                    capability.id,
                    stream_id,
                    address,
                    rate,
                    audio=handle,
                    tail=tail,
                )
                await self._open(controller, mic)
            if capability.kind == "speaker":
                self._think(controller, Moved())
            return handle

        return await controller.submit(op)

    async def speaker_start(
        self,
        session: Session,
        device: DeviceRecord,
        capability: Capability,
        *,
        on_starved: Callable[[], None] | None = None,
        on_end: Callable[[StreamEnd], None] | None = None,
    ) -> SpeakerStream:
        """Open a speaker stream that the hub feeds itself, at the device's rate. The agent
        gets no handle to it, and no `stream.*` notifications. It preempts as `stream_start`
        does. `on_starved` is called whenever it has sent every whole frame it was fed, and
        isn't draining; `on_end` as it ends, once it has started. Raises `HubError`."""
        self.speaker_check(device, capability)
        address = f"{device.name}/{capability.id}"
        controller = self._controller(device)

        async def op() -> SpeakerStream:
            self._require_current(session)
            _check_held(controller, address)
            conn = controller.conn
            stream_id = conn.stream_ids.open()
            stream = SpeakerStream(
                None, session, conn, capability.id, stream_id, address, FORMAT.rate
            )
            stream.on_starved = on_starved
            stream.on_end = on_end
            await self._open(controller, stream)
            self._think(controller, Moved())
            return stream

        return await controller.submit(op)

    async def mic_start(
        self,
        session: Session,
        device: DeviceRecord,
        capability: Capability,
        listener: Listener,
    ) -> MicStream:
        """Open a mic stream that the hub runs itself, for a listen, which `listener` hears.
        The agent gets no `stream.*` notifications for it: only its frames, on the listener's
        `audio` handle, if it has one. It preempts as `stream_start` does, and ends `thinking`
        on the device. Raises `HubError`."""
        address = f"{device.name}/{capability.id}"
        if not FORMAT.offered(capability.params):
            raise HubError("unsupported", f"{address} has no 16 kHz mono PCM in 10 ms frames")
        controller = self._controller(device)

        async def op() -> MicStream:
            self._require_current(session)
            conn = controller.conn
            stream_id = conn.stream_ids.open()
            tail = self._timing.mic_tail_ms * 1000
            stream = MicStream.listened(
                session, conn, capability.id, stream_id, address, listener, tail=tail
            )
            await self._open(controller, stream)
            self._think(controller, Moved())
            return stream

        return await controller.submit(op)

    def utterance_delivered(self, device: DeviceRecord) -> None:
        """An Utterance from `device` went to the agent: it shows `thinking`."""
        controller = self._controllers.get(device.device_id)
        if controller is not None:
            self._think(controller, Delivered())

    def speaker_check(self, device: DeviceRecord, capability: Capability) -> None:
        """Raise the `HubError` that `speaker_start` would before it tries: `unsupported` for
        a speaker that doesn't play the hub's audio, `disconnected` if it's offline."""
        address = f"{device.name}/{capability.id}"
        if not FORMAT.offered(capability.params):
            raise HubError("unsupported", f"{address} has no 16 kHz mono PCM in 10 ms frames")
        self._controller(device)

    async def _open(
        self,
        controller: Controller,
        stream: SpeakerStream | MicStream,
        prefill_ms: int | None = None,
    ) -> None:
        """Start `stream` on its device, and stop what it preempts first, pipelined: whatever
        is open on its capability, or on the capability's `exclusive` peers, ends `preempted`.
        Raises `HubError`, having given the stream id back."""
        conn = controller.conn
        args: JsonObject = {"stream": stream.stream_id, "format": FORMAT.to_json()}
        if isinstance(stream, SpeakerStream):
            args["prefill_ms"] = _PREFILL_MS if prefill_ms is None else prefill_ms
        else:
            conn.receivers[stream.stream_id] = stream  # its first frames may beat the reply
        preempted = [
            old
            for cap in _group(conn.manifest, stream.cap)
            if (old := controller.streams.get(cap)) is not None
        ]
        commands = [self._halt(old) for old in preempted]
        commands.append((stream.cap, "start", args))
        replies = await asyncio.gather(
            *(conn.command(*command) for command in commands), return_exceptions=True
        )
        await self._finish_all(preempted, "preempted", replies)
        started = replies[-1]
        if self._controllers.get(conn.manifest.device_id) is not controller:
            started = HubError("disconnected", f"{conn.name} went offline")
        if isinstance(started, BaseException):
            if isinstance(started, HubError) and started.code == "timeout":
                # The device may yet start it: the capability mustn't stay busy with it.
                with contextlib.suppress(HubError):
                    await conn.command(stream.cap, "stop", {})
            if conn.receivers.get(stream.stream_id) is stream:
                del conn.receivers[stream.stream_id]
            conn.stream_ids.close(stream.stream_id)
            raise started
        controller.streams[stream.cap] = stream
        if stream.handle is not None:
            stream.session.streams[stream.handle] = stream
            stream.session.notify(
                "stream.started",
                {"handle": stream.handle, "address": stream.address, "rate": stream.rate},
            )
        if isinstance(stream, SpeakerStream):
            self._speakers.open(stream)
        else:
            stream.started()

    def stream_feed(self, session: Session, handle: int, pcm: bytes) -> None:
        """Take the agent's audio for a stream. Audio for a stream that isn't open is
        dropped."""
        stream = session.streams.get(handle)
        if isinstance(stream, SpeakerStream):
            self.speaker_feed(stream, pcm)

    def speaker_feed(self, stream: SpeakerStream, pcm: bytes) -> None:
        """Take audio for a speaker stream, at its rate. Audio for a stream that isn't open is
        dropped."""
        if stream.open and self._speakers.feed(stream, pcm):
            self._halt(stream)
            self._submit_stop(stream, "overflow")

    def speaker_drain(self, stream: SpeakerStream) -> None:
        """No more audio follows: the stream ends once what it was fed has played."""
        if stream.open:
            self._speakers.drain(stream)

    def speaker_unsent(self, stream: SpeakerStream) -> int:
        """The bytes of a speaker stream's audio not sent to its device yet, at 16 kHz."""
        return self._speakers.unsent(stream)

    def speaker_starved(self, stream: SpeakerStream) -> bool:
        """Whether a speaker stream is open, and has sent every whole frame it was fed."""
        return self._speakers.starved(stream)

    def speaker_due(self, stream: SpeakerStream, position: int) -> int | None:
        """When the audio `position` bytes into a speaker stream, at 16 kHz, is due to play,
        in µs on the hub's monotonic clock; None before its first frame went out."""
        return self._speakers.due(stream, position)

    async def stream_stop(self, session: Session, handle: int) -> StreamEnd:
        """End a stream at once. Raises `HubError`: `disconnected` if it has ended."""
        return await self.stop(self._stream(session, handle))

    async def stop(self, stream: Stream) -> StreamEnd:
        """End a stream at once, with `stopped`. Returns how it ended, even if it had."""
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
        self.speaker_drain(stream)
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
            skipped = 0 if stream.audio is None else stream.session.audio_ended(stream.audio)
            stats = stream.stats(skipped=skipped)
        assert stats is not None
        conn.stream_ids.close(stream.stream_id)
        controller = self._controllers.get(conn.manifest.device_id)
        if controller is not None and controller.streams.get(stream.cap) is stream:
            del controller.streams[stream.cap]
        end = StreamEnd(reason, stats)
        _log.info("%s: stream %d ended %s: %s", stream.address, stream.stream_id, reason, stats)
        stream.ended.set_result(end)
        if stream.on_end is not None:
            stream.on_end(end)
        if stream.handle is not None:
            if stream.session.streams.get(stream.handle) is stream:
                del stream.session.streams[stream.handle]
            stream.session.notify(
                "stream.ended", {"handle": stream.handle, **end.to_json()}, after=stream.handle
            )

    def _gestures(
        self, controller: Controller, input: Down | Up | Tick, now: int, cap: str | None = None
    ) -> list[Gesture]:
        """Run the gestures of button `cap`, or of every button for a `Tick`, and send what
        they see to the Session. Returns what they saw."""
        conn = controller.conn
        seen: list[Gesture] = []
        for cap_id, gestures in controller.gestures.items():
            if cap is not None and cap_id != cap:
                continue
            for gesture in gestures.handle(input, now):
                seen.append(gesture)
                self._button_event(controller, cap_id, gesture.name, {}, gesture.ts)
        deadlines = [d for g in controller.gestures.values() if (d := g.next_deadline())]
        if self._current(conn) is controller:
            controller.gestures_alarm.set(min(deadlines, default=None), now)
        return seen

    def _gestures_due(self, controller: Controller, tick: Tick, now: int) -> None:
        self._gestures(controller, tick, now)

    def _button_event(
        self,
        controller: Controller,
        cap: str,
        name: str,
        data: JsonObject,
        at: int | None,
        *,
        edge: bool = False,
    ) -> None:
        """Send `button.<name>` to the Session, once the device is up to date. `at` is when it
        happened, in hub µs; `edge` marks a raw edge."""
        conn = controller.conn
        if (
            self._session is not None
            and controller.up_to_date
            and self._current(conn) is controller
        ):
            self._session.device_event(
                f"button.{name}", {"address": f"{conn.name}/{cap}", "data": data}, at, edge=edge
            )

    def _thinking(self) -> Thinking:
        return Thinking(timeout=self._timing.thinking_ms * 1000)

    def _think(
        self, controller: Controller, input: Delivered | Moved | Tick, now: int | None = None
    ) -> None:
        """Run the device's `thinking` core, and show what it says while the Session lasts."""
        now = now_us() if now is None else now
        for show in controller.thinking.handle(input, now):
            if self._session is not None and self._current(controller.conn) is controller:
                _log_failure(controller.conn.command(*_status(show.state)))
        controller.thinking_alarm.set(controller.thinking.next_deadline(), now)

    def _hub_status(self) -> _HubStatus:
        return "no_agent" if self._session is None else "agent"

    def _armed(self, device_id: str) -> bool:
        """Whether the device's Reflex is to be armed."""
        return self._session is not None and device_id not in self._disarmed

    def _current(self, conn: DeviceConnection) -> Controller | None:
        """The controller of `conn`, while it's the device's online Connection."""
        controller = self._controllers.get(conn.manifest.device_id)
        return controller if controller is not None and controller.conn is conn else None

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


def _reflex(manifest: Manifest, *, armed: bool) -> list[CommandSpec]:
    """Each button's `set_reflex`. Armed, it cuts every speaker, then opens `_reflex_mic`."""
    capabilities = manifest.capabilities
    args: JsonObject = {"armed": armed}
    if armed:
        if stop := [cap.id for cap in capabilities if cap.kind == "speaker"]:
            args["stop"] = stop
        if (mic := _reflex_mic(manifest)) is not None:
            args["start"] = {"cap": mic.id, "format": FORMAT.to_json()}
    return [(cap.id, "set_reflex", args) for cap in capabilities if cap.kind == "button"]


def _reflex_mic(manifest: Manifest) -> Capability | None:
    """The mic the Reflex opens: the first that captures the hub's format."""
    mics = (cap for cap in manifest.capabilities if cap.kind == "mic")
    return next((mic for mic in mics if FORMAT.offered(mic.params)), None)


def _kind(manifest: Manifest, cap: str) -> str | None:
    return next((c.kind for c in manifest.capabilities if c.id == cap), None)


def _check_held(controller: Controller, address: str) -> None:
    """Raises `busy` while the human holds the button for a mic of the device: the human
    wins."""
    if any(isinstance(s, MicStream) and s.held for s in controller.streams.values()):
        raise HubError("busy", f"{address}: the human holds the button to talk")


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
