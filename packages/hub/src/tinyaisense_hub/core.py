"""The core facade: the internal seam every front-end talks to.

The agent-facing front-ends (the agent API, and MCP later) only translate between their wire
and these calls. Behaviour lives here and below.
"""

import secrets

from tinyaisense_wire import JsonObject

from .devices import Registry
from .errors import HubError
from .session import Holder, Session, Sessions, SessionSink, UtteranceOptIn


class Core:
    def __init__(self, registry: Registry, sessions: Sessions, agent_token: str) -> None:
        self._registry = registry
        self._sessions = sessions
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
