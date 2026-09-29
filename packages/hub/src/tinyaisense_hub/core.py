"""The core facade: the internal seam every front-end talks to.

The agent-facing front-ends (the agent API, and MCP later) only translate between their wire
and these calls. Behaviour lives here and below.
"""

import secrets

from tinyaisense_wire import JsonObject

from .control import Control
from .devices import Registry
from .errors import HubError
from .session import Holder, Session, Sessions, SessionSink, UtteranceOptIn
from .speaker import FORMAT


class Core:
    def __init__(
        self, registry: Registry, sessions: Sessions, control: Control, agent_token: str
    ) -> None:
        self._registry = registry
        self._sessions = sessions
        self._control = control
        self._agent_token = agent_token.encode()

    async def open_session(
        self,
        token: str,
        holder: Holder,
        sink: SessionSink,
        *,
        takeover: bool,
        utterances: UtteranceOptIn,
    ) -> Session:
        """Claim the hub for an agent. Raises `HubError`: `unauthorized` or `busy`."""
        if not secrets.compare_digest(token.encode(), self._agent_token):
            raise HubError("unauthorized", "the agent token doesn't match")
        return await self._sessions.claim(holder, sink, takeover=takeover, utterances=utterances)

    async def close_session(self, session: Session) -> None:
        """The Session's link closed or went dead."""
        await self._sessions.end(session, "closed")

    async def shutdown(self) -> None:
        """End the Session, if any, and refuse new ones: the hub is going down."""
        await self._sessions.shutdown()

    def devices(self) -> list[JsonObject]:
        return self._registry.paired()

    async def command(
        self, session: Session, address: str, name: str, args: JsonObject
    ) -> JsonObject:
        """Send a Kind command to the capability at `address`. Raises `HubError`."""
        device, capability = self._registry.resolve(address)
        return await self._control.command(session, device, capability, name, args)

    async def led_release(self, session: Session, device: str) -> None:
        """Hand a device's LEDs back to its Status. Raises `HubError`."""
        await self._control.led_release(session, self._registry.named(device))

    async def stream_start(
        self, session: Session, address: str, rate: int | None, prefill_ms: int
    ) -> int:
        """Open a speaker stream to the capability at `address`. Returns its handle. Raises
        `HubError`."""
        device, capability = self._registry.resolve(address)
        if capability.kind == "mic":
            raise HubError("unsupported", "mic streams aren't there yet")
        if rate is not None and rate != FORMAT.rate:
            raise HubError("unsupported", f"the hub streams at {FORMAT.rate} Hz only, for now")
        return await self._control.stream_start(session, device, capability, prefill_ms)

    def stream_feed(self, session: Session, handle: int, pcm: bytes) -> None:
        """The agent's audio for a speaker stream. Dropped if it names no open one."""
        self._control.stream_feed(session, handle, pcm)

    async def stream_stop(self, session: Session, handle: int) -> JsonObject:
        """End a stream at once. Returns its `reason` and `stats`. Raises `HubError`."""
        return (await self._control.stream_stop(session, handle)).to_json()

    async def stream_drain(self, session: Session, handle: int) -> JsonObject:
        """End a speaker stream once what the agent sent has played. Returns its `reason` and
        `stats`. Raises `HubError`."""
        return (await self._control.stream_drain(session, handle)).to_json()
