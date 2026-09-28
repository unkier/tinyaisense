"""Sessions: one agent's claim on the hub, at most one at a time."""

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

from tinyaisense_wire import JsonObject, Manifest

from .errors import HubError

type Holder = Literal["agent_api", "mcp"]
"""Which front-end claimed the Session."""

type EndReason = Literal["closed", "taken_over", "shutdown"]
"""Why a Session ended: its link closed or went dead, another agent took over, or the hub
is shutting down."""

_log = logging.getLogger("tinyaisense_hub.session")


class SessionSink(Protocol):
    """Where a Session's notifications go: its agent-API link. Neither method may block."""

    def notify(self, method: str, params: JsonObject) -> None: ...

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


class Sessions:
    """The Session manager. It also turns device presence into notifications."""

    def __init__(self) -> None:
        self.current: Session | None = None
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
        if reason != "closed":  # else its link is gone
            session.sink.notify("session.ended", {"reason": reason, "t": _now_ms()})
        session.sink.ended(reason)

    def notify(self, method: str, params: JsonObject) -> None:
        """Send a notification to the Session, if there is one, stamped with `t`: now."""
        if self.current is not None:
            self.current.sink.notify(method, params | {"t": _now_ms()})

    # Device presence, from the registry.

    def device_online(self, name: str, manifest: Manifest) -> None:
        self.notify("device.online", {"device": name, "manifest": manifest.to_json()})

    def device_offline(self, name: str) -> None:
        self.notify("device.offline", {"device": name})


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _local_time(epoch_ms: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch_ms / 1000))
