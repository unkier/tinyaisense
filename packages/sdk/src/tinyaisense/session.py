"""A Session on the hub, over its agent API: docs/protocol/agent.md."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass
from types import TracebackType
from typing import Any, cast

from tinyaisense_wire import (
    AgentAudio,
    HubAudio,
    JsonObject,
    WireError,
    decode_hub_audio,
    encode_agent_audio,
)
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

PING_INTERVAL = 2.0
PING_TIMEOUT = 4.0
"""The SDK pings the hub too, so that it notices a dead hub within ~6 s."""
AUDIO_CHUNK = 32768
"""`send_audio` sends at most this many bytes of PCM per binary frame."""


class ApiError(Exception):
    """The hub refused a request. Branch on `code`, e.g. `busy` or `unknown_address`."""

    def __init__(self, code: str, message: str, data: JsonObject) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data
        """The error's `data`, `code` included: e.g. `holder` and `since` for `busy`."""


class SessionEnded(Exception):
    """The Session is over, so the request can't be made."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"the Session ended: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class Notification:
    method: str
    params: JsonObject

    @property
    def t(self) -> int:
        """The hub's wall clock, in epoch ms."""
        return cast(int, self.params["t"])


class Session:
    """A Session on the hub, over one agent-API link. Get one from `connect`.

    It never reconnects by itself. That matters after `taken_over`: reconnecting would take
    the hub back, and two agents would take it from each other forever.
    """

    def __init__(self, ws: ClientConnection) -> None:
        self.id = ""
        """The Session id, from `hello`. An MCP link joins with it in `X-Hub-Session`."""
        self.ended: str | None = None
        """Why the Session ended, once it has: `taken_over`, `shutdown`, or `closed` when
        the link dropped."""
        self._ws = ws
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[JsonObject]] = {}
        self._notifications: asyncio.Queue[Notification | None] = asyncio.Queue()
        self._audio: dict[int, asyncio.Queue[HubAudio | None]] = {}
        """Each stream's frames, by handle, from `stream.started` or `utterance.start` until
        read. None ends a stream's."""
        self._utterance_handles: dict[int, int] = {}
        """The handle of each Utterance whose audio is coming, by utterance id."""
        self._cancels: set[asyncio.Task[None]] = set()
        self._reader = asyncio.create_task(self._read())

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def call(self, method: str, **params: Any) -> JsonObject:
        """Make a request and return its result. Raises `ApiError` or `SessionEnded`.
        Cancelling it cancels the request on the hub, e.g. ends a `listen` `stopped`."""
        if self.ended is not None:
            raise SessionEnded(self.ended)
        request_id = next(self._ids)
        reply = self._pending[request_id] = asyncio.get_running_loop().create_future()
        try:
            await self._send(request_id, method, params)
            return await asyncio.shield(reply)
        except ConnectionClosed:
            raise SessionEnded(self.ended or "closed") from None
        except asyncio.CancelledError:
            if not reply.done() and self.ended is None:
                cancel = asyncio.create_task(self._cancel(request_id))
                self._cancels.add(cancel)
                cancel.add_done_callback(self._cancels.discard)
            raise
        finally:
            self._pending.pop(request_id, None)

    async def devices(self) -> list[JsonObject]:
        """Every paired device: `{name, online, manifest}`."""
        return cast(list[JsonObject], (await self.call("devices"))["devices"])

    async def voices(self) -> list[JsonObject]:
        """The Voices the hub speaks with: `{id, lang, name}`. Pass an `id` to `say`."""
        return cast(list[JsonObject], (await self.call("voices"))["voices"])

    async def command(self, address: str, name: str, args: JsonObject | None = None) -> JsonObject:
        """Send a Kind command to the capability at `address`, e.g. `kitchen/led`, and return
        its result. An `led` command claims the device's LED from its Status."""
        return await self.call("command", address=address, name=name, args=args or {})

    async def led_release(self, device: str) -> None:
        """Hand `device`'s LED back to its Status."""
        await self.call("led.release", device=device)

    async def reflex_set(self, device: str, armed: bool) -> None:
        """Arm or disarm `device`'s Reflex, barge-in and push-to-talk, for this Session. It's
        armed on every device by default."""
        await self.call("reflex.set", device=device, armed=armed)

    async def stream_start(
        self, address: str, *, rate: int | None = None, prefill_ms: int | None = None
    ) -> int:
        """Open a stream on the capability at `address`, e.g. `kitchen/speaker`, and return
        its handle. `rate` defaults to 16000; `prefill_ms`, for a speaker, to 40."""
        params: JsonObject = {"address": address}
        if rate is not None:
            params["rate"] = rate
        if prefill_ms is not None:
            params["prefill_ms"] = prefill_ms
        return cast(int, (await self.call("stream.start", **params))["handle"])

    async def send_audio(self, handle: int, pcm: bytes) -> None:
        """Send PCM (s16le mono, at the stream's rate) to a speaker stream, as fast as you
        get it: the hub paces it. Raises `SessionEnded`, or `ValueError` for PCM that isn't a
        whole number of samples."""
        if len(pcm) % 2:
            raise ValueError(f"{len(pcm)} bytes isn't a whole number of 16-bit samples")
        if self.ended is not None:
            raise SessionEnded(self.ended)
        try:
            for at in range(0, len(pcm), AUDIO_CHUNK):
                chunk = pcm[at : at + AUDIO_CHUNK]
                await self._ws.send(encode_agent_audio(AgentAudio(handle, chunk)))
        except ConnectionClosed:
            raise SessionEnded(self.ended or "closed") from None

    async def stream_stop(self, handle: int) -> JsonObject:
        """End a stream at once. Returns `{reason, stats}`."""
        return await self.call("stream.stop", handle=handle)

    async def stream_drain(self, handle: int) -> JsonObject:
        """End a speaker stream once what was sent has played. Returns `{reason, stats}`."""
        return await self.call("stream.drain", handle=handle)

    async def say(
        self,
        address: str,
        text: str,
        *,
        voice: str | None = None,
        lang: str | None = None,
        replace: bool = False,
        then_listen: bool | JsonObject = False,
    ) -> JsonObject:
        """Speak `text` on the speaker at `address`, e.g. `kitchen/speaker`. Returns once it
        has played, or been cut: `{reason, played_ms, interrupted, heard_text}`, where
        `heard_text` is what the human heard of it.

        A `say` queued on a speaker plays right after the ones before it; with `replace`, it
        cuts them off instead, and they end `preempted`. `voice` is a Voice id from `voices`.
        Without one, the hub speaks in its default Voice for `lang`, or for the language the
        text is written in.

        `then_listen` listens on the device's mic once it has played to its end, as `listen`
        does, and the result carries the `utterance_id` it comes as. It may be `{lang,
        utterances}`, as `listen` takes; or `{raw: true}`, for a raw mic stream instead,
        which comes as a `stream.started`."""
        params: JsonObject = {"address": address, "text": text}
        _say_params(params, voice, lang, replace, then_listen)
        return await self.call("say", **params)

    async def say_open(
        self,
        address: str,
        *,
        voice: str | None = None,
        lang: str | None = None,
        replace: bool = False,
        then_listen: bool | JsonObject = False,
    ) -> int:
        """Start speaking text that comes in pieces, e.g. an LLM's reply as it streams, on the
        speaker at `address`. Returns its handle: pass the text to `say_append`, then call
        `say_close`. Speech starts after the first clause, not after the whole text.

        It's queued, or cuts in with `replace`, as `say` is, and takes `then_listen` as `say`
        does. Without a `voice` or `lang`, the hub speaks in its default Voice for the
        language of the first clause. Close it, or the `say`s queued after it never play."""
        params: JsonObject = {"address": address}
        _say_params(params, voice, lang, replace, then_listen)
        return cast(int, (await self.call("say.open", **params))["handle"])

    async def say_append(self, handle: int, text: str) -> None:
        """More text for the `say` `handle`. Once it has ended, e.g. cut by the button, the
        text is ignored."""
        await self.call("say.append", handle=handle, text=text)

    async def say_close(self, handle: int) -> JsonObject:
        """No more text comes for the `say` `handle`. Returns once it has played, or been cut,
        what `say` returns."""
        return await self.call("say.close", handle=handle)

    async def listen(
        self, address: str, *, lang: str | None = None, utterances: JsonObject | None = None
    ) -> JsonObject:
        """Hear what the human says on the mic at `address`, e.g. `kitchen/mic`, until their
        turn is over. Returns the `utterance.end` params: `{utterance_id, reason, transcript,
        lang, stats}`, the transcript and lang if the Session opted in to transcripts.

        `lang` is the language to hear, if known. `utterances`, `{transcript, audio, rate}`,
        overrides the Session's opt-in for this one; its audio comes on the `handle` in
        `utterance.start`, to read with `audio`. Cancelling the call ends it `stopped`: what
        was heard so far still comes, in `utterance.end`."""
        params: JsonObject = {"address": address}
        if lang is not None:
            params["lang"] = lang
        if utterances is not None:
            params["utterances"] = utterances
        return await self.call("listen", **params)

    async def audio(self, handle: int) -> AsyncIterator[HubAudio]:
        """A mic stream's frames, or an Utterance's, in order, until it ends: 10 ms of PCM
        each (s16le mono, at the stream's rate), with its `index` on the stream's timeline. The hub
        zero-fills audio lost on the network, so a gap in `index` means that the hub skipped
        frames because they weren't read in time. Read each stream's frames once: they queue
        up until they are."""
        queue = self._audio.get(handle)
        if queue is None:
            return  # it has ended, and its frames were read
        try:
            while (frame := await queue.get()) is not None:
                yield frame
        finally:
            if self._audio.get(handle) is queue:
                del self._audio[handle]

    async def notifications(self) -> AsyncIterator[Notification]:
        """The Session's notifications, in order, until it ends. `session.ended`, if the hub
        sends it, comes last. Only one consumer gets each notification."""
        while (notification := await self._notifications.get()) is not None:
            yield notification
        self._notifications.put_nowait(None)  # for the next consumer

    async def close(self) -> None:
        """Leave: the Session ends."""
        await self._ws.close()
        await self._reader

    async def _read(self) -> None:
        try:
            async for message in self._ws:
                if isinstance(message, str):
                    self._receive(cast(JsonObject, json.loads(message)))
                else:
                    self._receive_audio(message)
        except ConnectionClosed:
            pass
        finally:
            self.ended = self.ended or "closed"
            for reply in self._pending.values():
                if not reply.done():
                    reply.set_exception(SessionEnded(self.ended))
            self._notifications.put_nowait(None)
            for queue in self._audio.values():
                queue.put_nowait(None)

    def _audio_ended(self, handle: int) -> None:
        if (queue := self._audio.pop(handle, None)) is not None:
            if not queue.empty():
                self._audio[handle] = queue  # for the reader still to come
            queue.put_nowait(None)

    async def _send(self, request_id: int, method: str, params: JsonObject) -> None:
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        await self._ws.send(json.dumps(message, separators=(",", ":"), ensure_ascii=False))

    async def _cancel(self, request_id: int) -> None:
        """Cancel a request, not waiting for the hub's reply."""
        with contextlib.suppress(ConnectionClosed):
            await self._send(next(self._ids), "cancel", {"id": request_id})

    def _receive_audio(self, data: bytes) -> None:
        try:
            frame = decode_hub_audio(data)
        except WireError:
            return
        # The hub sends a stream's frames after its `stream.started`, and before its end.
        if (queue := self._audio.get(frame.handle)) is not None:
            queue.put_nowait(frame)

    def _receive(self, message: JsonObject) -> None:
        if "method" in message:
            notification = Notification(message["method"], message.get("params", {}))
            if notification.method == "session.ended":
                self.ended = notification.params["reason"]
            elif notification.method == "stream.started":
                self._audio[notification.params["handle"]] = asyncio.Queue()
            elif notification.method == "stream.ended":
                self._audio_ended(notification.params["handle"])
            elif notification.method == "utterance.start":
                if (handle := notification.params.get("handle")) is not None:
                    self._utterance_handles[notification.params["utterance_id"]] = handle
                    self._audio[handle] = asyncio.Queue()
            elif notification.method == "utterance.end":
                utterance_id = notification.params["utterance_id"]
                if (handle := self._utterance_handles.pop(utterance_id, None)) is not None:
                    self._audio_ended(handle)
            self._notifications.put_nowait(notification)
            return
        reply = self._pending.get(message.get("id", -1))
        if reply is None or reply.done():
            return
        if "error" in message:
            error = cast(JsonObject, message["error"])
            data = cast(JsonObject, error.get("data", {}))
            reply.set_exception(ApiError(data.get("code", "failed"), error["message"], data))
        else:
            reply.set_result(message["result"])


def _say_params(
    params: JsonObject,
    voice: str | None,
    lang: str | None,
    replace: bool,
    then_listen: bool | JsonObject,
) -> None:
    if voice is not None:
        params["voice"] = voice
    if lang is not None:
        params["lang"] = lang
    if replace:
        params["replace"] = True
    if then_listen:
        params["then_listen"] = then_listen


class _Connect:
    """`await connect(...)`, or `async with connect(...) as agent:`."""

    def __init__(self, url: str, token: str, takeover: bool, utterances: JsonObject | None) -> None:
        self._url = url
        self._params: JsonObject = {"token": token}
        if takeover:
            self._params["takeover"] = True
        if utterances is not None:
            self._params["utterances"] = utterances
        self._session: Session | None = None

    def __await__(self) -> Generator[Any, None, Session]:
        return self._connect().__await__()

    async def __aenter__(self) -> Session:
        self._session = await self._connect()
        return self._session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._session is not None:
            await self._session.close()

    async def _connect(self) -> Session:
        ws = await ws_connect(
            self._url, compression=None, ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT
        )
        session = Session(ws)
        try:
            session.id = (await session.call("hello", **self._params))["session"]
        except BaseException:
            with contextlib.suppress(Exception):
                await session.close()
            raise
        return session


def connect(
    url: str, token: str, *, takeover: bool = False, utterances: JsonObject | None = None
) -> _Connect:
    """Open a link to the hub at `url` (`ws://<hub>:8421/agent`) and claim the Session.

    `takeover` ends the Session of any other agent first. `utterances` sets the Session's
    default Utterance opt-in, `{transcript, audio, rate}`. Raises `ApiError`: `unauthorized`
    for a wrong token, `busy` if another agent holds the hub.
    """
    return _Connect(url, token, takeover, utterances)
