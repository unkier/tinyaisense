"""Sessions: one agent's claim on the hub, at most one at a time."""

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from tinyaisense_wire import JsonObject

from .errors import HubError
from .streams import Stream

if TYPE_CHECKING:
    from .speech.say import Say  # a Session holds its open says

type Holder = Literal["agent_api", "mcp"]
"""Which front-end claimed the Session."""

type EndReason = Literal["closed", "taken_over", "shutdown"]
"""Why a Session ended: its link closed or went dead, another agent took over, or the hub
is shutting down."""

_MAX_HANDLE = 65535

_log = logging.getLogger("tinyaisense_hub.session")


class SessionSink(Protocol):
    """Where a Session's notifications and audio go: its agent-API link. No method may
    block."""

    def notify(self, method: str, params: JsonObject) -> None: ...

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


@dataclass(eq=False, slots=True)
class Session:
    holder: Holder
    sink: SessionSink
    utterances: UtteranceOptIn
    id: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    since: int = field(default_factory=lambda: _now_ms())
    """Epoch ms."""
    streams: dict[int, Stream] = field(default_factory=dict[int, Stream])
    """The handle table: the Session's open streams, by handle."""
    says: dict[int, Say] = field(default_factory=dict[int, "Say"])
    """The Session's streaming `say`s, by handle, from `say.open` until `say.close`. They
    share the handles of streams."""
    _last_handle: int = field(default=0, init=False)

    def notify(self, method: str, params: JsonObject) -> None:
        """Send a notification, stamped with `t`: now."""
        self.sink.notify(method, params | {"t": _now_ms()})

    def audio(self, handle: int, index: int, pcm: bytes) -> None:
        self.sink.audio(handle, index, pcm)

    def audio_ended(self, handle: int) -> int:
        return self.sink.audio_ended(handle)

    def new_handle(self) -> int:
        """The next u16 handle after the last one used, round-robin from 1, skipping open
        ones. Raises `HubError`: `busy` if every handle is open."""
        for step in range(1, _MAX_HANDLE + 1):
            handle = (self._last_handle + step - 1) % _MAX_HANDLE + 1
            if handle not in self.streams and handle not in self.says:
                self._last_handle = handle
                return handle
        raise HubError("busy", "every stream handle is open")


class DeviceControl(Protocol):
    """The per-device controllers, told when a Session starts and ends."""

    def session_started(self, session: Session) -> None: ...

    async def session_ended(self, session: Session) -> None:
        """Undo on the devices what the Session did there."""
        ...


class Sessions:
    """The Session manager."""

    def __init__(self, control: DeviceControl) -> None:
        self.current: Session | None = None
        self._control = control
        self._lock = asyncio.Lock()
        """Held while a Session starts or ends, so that a takeover ends the old one first."""
        self._shut_down = False

    async def claim(
        self, holder: Holder, sink: SessionSink, *, takeover: bool, utterances: UtteranceOptIn
    ) -> Session:
        """Start a Session. Raises `HubError`: `busy` if one exists and not `takeover`, or
        `failed` once the hub is shutting down."""
        async with self._lock:
            if self._shut_down:
                raise HubError("failed", "the hub is shutting down")
            if (old := self.current) is not None:
                if not takeover:
                    raise HubError(
                        "busy",
                        f"another agent holds the hub, since {_local_time(old.since)}",
                        holder=old.holder,
                        since=old.since,
                    )
                await self._end(old, "taken_over")
            session = self.current = Session(holder, sink, utterances)
            _log.info("Session %s started (%s)", session.id, holder)
            self._control.session_started(session)
            return session

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

    async def _end(self, session: Session, reason: EndReason) -> None:
        self.current = None
        _log.info("Session %s ended: %s", session.id, reason)
        await self._control.session_ended(session)
        if reason != "closed":  # else its link is gone
            session.notify("session.ended", {"reason": reason})
        session.sink.ended(reason)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _local_time(epoch_ms: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch_ms / 1000))
