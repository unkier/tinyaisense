"""Sessions: one agent's claim on the hub, at most one at a time."""

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from tinyaisense_wire import JsonObject

from .alarm import Alarm, epoch_ms, now_us
from .config import Timing
from .errors import HubError
from .idle import CallEnded, CallStarted, Idle
from .keepalive import Tick
from .record import Recorder, SessionRecording, TracedCall
from .streams import Stream

if TYPE_CHECKING:
    from .speech.listen import Listen  # a Session holds its listens
    from .speech.say import Say  # and its open says

type Holder = Literal["agent_api", "mcp"]
"""Which front-end claimed the Session."""

type EndReason = Literal["closed", "taken_over", "shutdown", "idle"]
"""Why a Session ended: its link closed or went dead, another agent took over, the hub is
shutting down, or an MCP-only Session sat idle."""

_MAX_HANDLE = 65535

_log = logging.getLogger("tinyaisense_hub.session")


class SessionSink(Protocol):
    """Where a Session's notifications and audio go: its agent-API link, or an MCP-only
    Session's Event queue; and what follows them for the MCP links that joined an agent-API
    Session. No method may block."""

    def notify(
        self,
        method: str,
        params: JsonObject,
        after: int | None = None,
        latency_from: int | None = None,
    ) -> None:
        """Send a notification; `after` a stream handle, behind the frames of it queued so
        far. Given `latency_from`, a time on the hub's monotonic clock in µs, the time from
        then until the notification is written to the socket counts toward the button latency
        stats."""
        ...

    def audio(self, handle: int, index: int, pcm: bytes) -> None:
        """Frame `index` of stream `handle`'s timeline."""
        ...

    def audio_ended(self, handle: int) -> int:
        """No more audio follows on `handle`: what's queued of it goes out before whatever is
        sent next. Returns how many of its frames were skipped, unread."""
        ...

    def ended(self, reason: EndReason) -> None:
        """The Session is over. Called once, after its last notification."""
        ...


@dataclass(frozen=True, slots=True)
class UtteranceOptIn:
    """What an Utterance delivers: docs/protocol/agent.md#utterances."""

    transcript: bool = True
    audio: bool = False
    rate: int = 16000


@dataclass(frozen=True, slots=True)
class ThenListen:
    """What a `say` with `then_listen` does once it has played: listen as `listen` does,
    or open a `raw` mic stream at `utterances.rate`."""

    raw: bool
    lang: str | None
    utterances: UtteranceOptIn


@dataclass(eq=False, slots=True)
class McpOnly:
    """What an MCP-only Session holds besides: which agent holds it, and the idle timer that
    releases it."""

    identity: str
    """The agent that holds it: its MCP session, or its MCP client's name and address."""
    idle: Idle
    alarm: Alarm
    released: bool = False
    """It went idle: its end is under way, and no call takes it up again."""

    def takes(self, identity: str) -> bool:
        """Whether a call of the agent `identity` runs in it."""
        return self.identity == identity and not self.released


@dataclass(eq=False, slots=True)
class Session:
    holder: Holder
    sink: SessionSink
    utterances: UtteranceOptIn
    mcp: McpOnly | None = None
    """Set for a Session that MCP claimed, with no agent-API link."""
    joined: SessionSink | None = None
    """What also hears the Session's notifications, but not its audio: the MCP links that
    joined it, once one has."""
    recording: SessionRecording | None = None
    """Its dir under the record dir, if the hub records Sessions."""
    id: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    since: int = field(default_factory=lambda: _now_ms())
    """Epoch ms."""
    streams: dict[int, Stream] = field(default_factory=dict[int, Stream])
    """The handle table: the Session's open streams, by handle."""
    says: dict[int, Say] = field(default_factory=dict[int, "Say"])
    """The Session's streaming `say`s, by handle, from `say.open` until `say.close`. They
    share the handles of streams."""
    listens: set[Listen] = field(default_factory=set["Listen"])
    """The Session's listens, until their Utterances end. The handle of one that delivers
    audio is taken, as a stream's is."""
    _last_handle: int = field(default=0, init=False)
    _last_utterance: int = field(default=0, init=False)

    def notify(self, method: str, params: JsonObject, after: int | None = None) -> None:
        """Send a notification, stamped with `t`: now. `after` a stream handle, it follows
        the frames of it queued so far."""
        self._send(method, params | {"t": _now_ms()}, after=after)

    def device_event(
        self, method: str, params: JsonObject, at: int | None, *, edge: bool = False
    ) -> None:
        """Send a device Event, stamped with `t`: `at`, when it happened on the hub's
        monotonic clock in µs; now if None. A raw button `edge` counts toward the button
        latency stats."""
        t = _now_ms() if at is None else epoch_ms(at)
        self._send(method, params | {"t": t}, latency_from=at if edge else None)

    def trace_call(self, via: Holder, method: str, params: JsonObject) -> TracedCall:
        """Trace an agent's call, if the Session is recorded: then its reply, once it ends."""
        if self.recording is None:
            return TracedCall(None, 0)
        return self.recording.call(via, method, params)

    def audio(self, handle: int, index: int, pcm: bytes) -> None:
        self.sink.audio(handle, index, pcm)

    def audio_ended(self, handle: int) -> int:
        return self.sink.audio_ended(handle)

    def new_handle(self) -> int:
        """The next u16 handle after the last one used, round-robin from 1, skipping open
        ones. Raises `HubError`: `busy` if every handle is open."""
        listening = {listen.handle for listen in self.listens}
        for step in range(1, _MAX_HANDLE + 1):
            handle = (self._last_handle + step - 1) % _MAX_HANDLE + 1
            if handle not in self.streams and handle not in self.says and handle not in listening:
                self._last_handle = handle
                return handle
        raise HubError("busy", "every stream handle is open")

    def new_utterance_id(self) -> int:
        self._last_utterance += 1
        return self._last_utterance

    def _send(
        self,
        method: str,
        params: JsonObject,
        after: int | None = None,
        latency_from: int | None = None,
    ) -> None:
        if self.recording is not None:
            self.recording.notify(method, params)
        self.sink.notify(method, params, after, latency_from)
        if self.joined is not None:
            self.joined.notify(method, params)


class DeviceControl(Protocol):
    """The per-device controllers, told when a Session starts and ends."""

    def session_started(self, session: Session) -> None: ...

    async def session_ended(self, session: Session) -> None:
        """Undo on the devices what the Session did there."""
        ...


class SpeechControl(Protocol):
    """The speech layer, told when a Session ends."""

    async def session_ended(self, session: Session) -> None:
        """End the Session's listens, once their Utterances are delivered."""
        ...


class Sessions:
    """The Session manager."""

    def __init__(
        self,
        control: DeviceControl,
        speech: SpeechControl,
        timing: Timing,
        recorder: Recorder | None = None,
    ) -> None:
        """`recorder` records every Session, if given."""
        self.current: Session | None = None
        self._recorder = recorder
        self._control = control
        self._speech = speech
        self._idle_after = timing.mcp_idle_ms * 1000
        self._lock = asyncio.Lock()
        """Held while a Session starts or ends, so that a takeover ends the old one first."""
        self._shut_down = False
        self._tasks: set[asyncio.Task[None]] = set()

    async def claim(
        self, holder: Holder, sink: SessionSink, *, takeover: bool, utterances: UtteranceOptIn
    ) -> Session:
        """Start a Session. Raises `HubError`: `busy` if one exists and not `takeover`, or
        `failed` once the hub is shutting down."""
        async with self._lock:
            await self._free(takeover)
            return self._start(Session(holder, sink, utterances))

    @asynccontextmanager
    async def mcp_call(
        self, identity: str, sink: Callable[[], SessionSink]
    ) -> AsyncGenerator[Session]:
        """Run a tool call of the agent `identity` in its MCP-only Session, which it claims,
        with a new `sink`, if no Session holds the hub. The Session ends once no call of it has
        run for `mcp_idle_ms`. Raises `HubError`: `busy` if another Session holds the hub, or
        `failed` once the hub is shutting down."""
        async with self._lock:
            session = self.mcp_session(identity)
            if session is None:
                await self._free(takeover=False)
                session = Session("mcp", sink(), UtteranceOptIn())
                session.mcp = McpOnly(
                    identity,
                    Idle(after=self._idle_after),
                    Alarm(lambda now: self.mcp_idle(session, Tick(), now)),
                )
                self._start(session)
        self.mcp_idle(session, CallStarted())
        try:
            yield session
        finally:
            self.mcp_idle(session, CallEnded())

    def joined(self, session_id: str) -> Session:
        """The Session `session_id`, which an MCP link joins. Raises `HubError`:
        `disconnected` if it isn't the one that holds the hub."""
        session = self.current
        if session is None or session.id != session_id:
            raise HubError("disconnected", "the Session it joined has ended")
        return session

    def mcp_session(self, identity: str) -> Session | None:
        """The MCP-only Session of the agent `identity`, if it holds the hub."""
        session = self.current
        if session is not None and session.mcp is not None and session.mcp.takes(identity):
            return session
        return None

    def mcp_idle(
        self, session: Session, input: CallStarted | CallEnded | Tick, now: int | None = None
    ) -> None:
        """Run the idle timer of the MCP-only `session`, while it holds the hub."""
        if self.current is not session or (mcp := session.mcp) is None:
            return
        now = now_us() if now is None else now
        if mcp.idle.handle(input, now):
            _log.info("Session %s is idle", session.id)
            mcp.released = True
            self._spawn(self.end(session, "idle"))
        mcp.alarm.set(mcp.idle.next_deadline(), now)

    async def mcp_release(self, identity: str) -> None:
        """End the MCP-only Session of the agent `identity`, if it holds the hub."""
        session = self.current
        if session is not None and session.mcp is not None and session.mcp.identity == identity:
            await self.end(session, "closed")

    async def end(self, session: Session, reason: EndReason) -> None:
        """End `session`, if it's still the current one."""
        async with self._lock:
            if self.current is session:
                await self._end(session, reason)

    async def shutdown(self) -> None:
        """End the Session, if any, and refuse new ones."""
        async with self._lock:
            self._shut_down = True
            if self.current is not None:
                await self._end(self.current, "shutdown")

    async def _free(self, takeover: bool) -> None:
        """Make way for a new Session: end the one that holds the hub, if `takeover` or it went
        idle. Raises
        `HubError`: `busy` if one holds it and not `takeover`, or `failed` once the hub is
        shutting down."""
        if self._shut_down:
            raise HubError("failed", "the hub is shutting down")
        if (old := self.current) is not None and old.mcp is not None and old.mcp.released:
            await self._end(old, "idle")  # ahead of its own end
        elif old is not None:
            if not takeover:
                raise HubError(
                    "busy",
                    f"another agent holds the hub, since {_local_time(old.since)}",
                    holder=old.holder,
                    since=old.since,
                )
            await self._end(old, "taken_over")

    def _start(self, session: Session) -> Session:
        self.current = session
        _log.info("Session %s started (%s)", session.id, session.holder)
        if self._recorder is not None:
            utterances = asdict(session.utterances)
            session.recording = self._recorder.session(
                session.id, session.holder, session.since, utterances
            )
        self._control.session_started(session)
        return session

    async def _end(self, session: Session, reason: EndReason) -> None:
        self.current = None
        if session.mcp is not None:
            session.mcp.alarm.cancel()
        _log.info("Session %s ended: %s", session.id, reason)
        await self._control.session_ended(session)
        await self._speech.session_ended(session)
        if reason != "closed":  # else its link is gone
            session.notify("session.ended", {"reason": reason})
        session.sink.ended(reason)
        if session.joined is not None:
            session.joined.ended(reason)
        if session.recording is not None:
            session.recording.close(reason)

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _local_time(epoch_ms: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch_ms / 1000))
