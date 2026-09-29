"""The core facade: the internal seam every front-end talks to.

The agent-facing front-ends (the agent API, and MCP later) only translate between their wire
and these calls. Behaviour lives here and below.
"""

import asyncio
import secrets

from tinyaisense_wire import Capability, JsonObject

from .control import Control
from .devices import Registry
from .errors import HubError
from .session import Holder, Session, Sessions, SessionSink, ThenListen, UtteranceOptIn
from .speech.layer import Speech
from .state import DeviceRecord


class Core:
    def __init__(
        self,
        registry: Registry,
        sessions: Sessions,
        control: Control,
        speech: Speech,
        agent_token: str,
    ) -> None:
        self._registry = registry
        self._sessions = sessions
        self._control = control
        self._speech = speech
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

    def voices(self) -> list[JsonObject]:
        return self._speech.voices()

    async def command(
        self, session: Session, address: str, name: str, args: JsonObject
    ) -> JsonObject:
        """Send a Kind command to the capability at `address`. Raises `HubError`."""
        device, capability = self._registry.resolve(address)
        return await self._control.command(session, device, capability, name, args)

    async def led_release(self, session: Session, device: str) -> None:
        """Hand a device's LEDs back to its Status. Raises `HubError`."""
        await self._control.led_release(session, self._registry.named(device))

    async def reflex_set(self, session: Session, device: str, armed: bool) -> None:
        """Arm or disarm a device's Reflex for the Session. Raises `HubError`."""
        await self._control.reflex_set(session, self._registry.named(device), armed)

    async def stream_start(
        self, session: Session, address: str, rate: int | None, prefill_ms: int | None
    ) -> int:
        """Open a stream to the speaker, or from the mic, at `address`. Returns its handle.
        Raises `HubError`."""
        device, capability = self._registry.resolve(address)
        return await self._control.stream_start(session, device, capability, rate, prefill_ms)

    def stream_feed(self, session: Session, handle: int, pcm: bytes) -> None:
        """The agent's audio for a speaker stream. Dropped if it names no open one."""
        self._control.stream_feed(session, handle, pcm)

    async def stream_stop(self, session: Session, handle: int) -> JsonObject:
        """End a stream at once. Returns its `reason` and `stats`. Raises `HubError`."""
        return (await self._control.stream_stop(session, handle)).to_json()

    async def stream_drain(self, session: Session, handle: int) -> JsonObject:
        """End a speaker stream once what the agent sent has played. Returns its `reason` and
        `stats`. Raises `HubError`, `unsupported` for a mic stream."""
        return (await self._control.stream_drain(session, handle)).to_json()

    async def say(
        self,
        session: Session,
        address: str,
        text: str,
        *,
        voice: str | None,
        lang: str | None,
        replace: bool,
        then_listen: ThenListen | None = None,
    ) -> JsonObject:
        """Speak `text` on the speaker at `address`, after what's queued on it, or cutting it
        off if `replace`; then, if it plays to the end, listen on the device's mic as
        `then_listen` says. Returns how it went once it has played. Raises `HubError`."""
        device, capability = self._speaker(address)
        return await self._speech.say(
            session,
            device,
            capability,
            text,
            voice=voice,
            lang=lang,
            replace=replace,
            then_listen=then_listen,
        )

    def say_open(
        self,
        session: Session,
        address: str,
        *,
        voice: str | None,
        lang: str | None,
        replace: bool,
        then_listen: ThenListen | None = None,
    ) -> int:
        """Start a `say` on the speaker at `address` whose text comes in with `say_append`,
        queued, and listening then, as `say` would be. Returns its handle. Raises
        `HubError`."""
        device, capability = self._speaker(address)
        return self._speech.say_open(
            session,
            device,
            capability,
            voice=voice,
            lang=lang,
            replace=replace,
            then_listen=then_listen,
        )

    def say_append(self, session: Session, handle: int, text: str) -> None:
        """More text for the open `say` `handle`. Raises `HubError`."""
        self._speech.say_append(session, handle, text)

    async def say_close(self, session: Session, handle: int) -> JsonObject:
        """No more text comes for the open `say` `handle`. Returns how it went once it has
        played. Raises `HubError`."""
        return await self._speech.say_close(session, handle)

    async def listen(
        self,
        session: Session,
        address: str,
        *,
        lang: str | None,
        utterances: UtteranceOptIn,
        stop: asyncio.Event | None = None,
    ) -> tuple[JsonObject, int | None]:
        """Hear an Utterance on the mic at `address`, in `lang` if given, delivered as
        `utterances` says. Returns the `utterance.end` params once it's delivered, and the
        handle its audio went to the agent on, if it did. Setting `stop` ends it `stopped`.
        Raises `HubError`."""
        device, capability = self._registry.resolve(address)
        if capability.kind != "mic":
            raise HubError("unsupported", f"{address} isn't a mic")
        return await self._speech.listen(
            session, device, capability, lang=lang, utterances=utterances, stop=stop
        )

    def _speaker(self, address: str) -> tuple[DeviceRecord, Capability]:
        """The speaker at `address`. Raises `HubError`."""
        device, capability = self._registry.resolve(address)
        if capability.kind != "speaker":
            raise HubError("unsupported", f"{address} isn't a speaker")
        return device, capability
