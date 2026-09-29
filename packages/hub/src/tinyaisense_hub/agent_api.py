"""The agent API front-end: JSON-RPC 2.0 over the `/agent` WebSocket.

docs/protocol/agent.md is the contract. This module only translates between it and the core
facade: it parses and checks requests, calls the core, and writes replies and notifications.
"""

import asyncio
import functools
import json
import logging
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, cast

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from tinyaisense_wire import (
    HubAudio,
    JsonObject,
    Schema,
    SchemaError,
    WireError,
    decode_agent_audio,
    encode_hub_audio,
    validate,
)

from .config import Timing
from .core import Core
from .errors import HubError, HubErrorCode
from .resampler import RATES
from .session import EndReason, Session, ThenListen, UtteranceOptIn

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_HUB_ERROR = -32000

_NORMAL = 1000
_GOING_AWAY = 1001
_POLICY = 1008
"""WebSocket close codes."""

_RATES = list(RATES)
_UTTERANCES: Schema = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "transcript": {"type": "boolean"},
        "audio": {"type": "boolean"},
        "rate": {"type": "integer", "enum": _RATES},
    },
}
_HELLO: Schema = {
    "type": "object",
    "required": ["token"],
    "additionalProperties": False,
    "properties": {
        "token": {"type": "string"},
        "takeover": {"type": "boolean"},
        "utterances": _UTTERANCES,
    },
}
_NO_PARAMS: Schema = {"type": "object", "additionalProperties": False}
_COMMAND: Schema = {
    "type": "object",
    "required": ["address", "name"],
    "additionalProperties": False,
    "properties": {
        "address": {"type": "string"},
        "name": {"type": "string"},
        "args": {"type": "object"},
    },
}
_DEVICE: Schema = {
    "type": "object",
    "required": ["device"],
    "additionalProperties": False,
    "properties": {"device": {"type": "string"}},
}

_STREAM_START: Schema = {
    "type": "object",
    "required": ["address"],
    "additionalProperties": False,
    "properties": {
        "address": {"type": "string"},
        "rate": {"type": "integer", "enum": _RATES},
        "prefill_ms": {"type": "integer", "minimum": 0, "maximum": 200},
    },
}
_HANDLE: Schema = {
    "type": "object",
    "required": ["handle"],
    "additionalProperties": False,
    "properties": {"handle": {"type": "integer", "minimum": 0, "maximum": 65535}},
}
_THEN_LISTEN: Schema = {
    "type": ["boolean", "object"],
    "additionalProperties": False,
    "properties": {
        "raw": {"type": "boolean"},
        "lang": {"type": "string"},
        "utterances": _UTTERANCES,
    },
}
_SAY: Schema = {
    "type": "object",
    "required": ["address", "text"],
    "additionalProperties": False,
    "properties": {
        "address": {"type": "string"},
        "text": {"type": "string"},
        "voice": {"type": "string"},
        "lang": {"type": "string"},
        "replace": {"type": "boolean"},
        "then_listen": _THEN_LISTEN,
    },
}
_SAY_OPEN: Schema = {
    "type": "object",
    "required": ["address"],
    "additionalProperties": False,
    "properties": {
        "address": {"type": "string"},
        "voice": {"type": "string"},
        "lang": {"type": "string"},
        "replace": {"type": "boolean"},
        "then_listen": _THEN_LISTEN,
    },
}
_SAY_APPEND: Schema = {
    "type": "object",
    "required": ["handle", "text"],
    "additionalProperties": False,
    "properties": {
        "handle": {"type": "integer", "minimum": 0, "maximum": 65535},
        "text": {"type": "string"},
    },
}
_LISTEN: Schema = {
    "type": "object",
    "required": ["address"],
    "additionalProperties": False,
    "properties": {
        "address": {"type": "string"},
        "lang": {"type": "string"},
        "utterances": _UTTERANCES,
    },
}
_CANCEL: Schema = {
    "type": "object",
    "required": ["id"],
    "additionalProperties": False,
    "properties": {"id": {"type": ["string", "integer"]}},
}
_log = logging.getLogger("tinyaisense_hub.agent_api")


def agent_app(core: Core, timing: Timing) -> Starlette:
    async def agent(websocket: WebSocket) -> None:
        await AgentLink(websocket, core, timing).run()

    return Starlette(routes=[WebSocketRoute("/agent", agent)])


@dataclass(frozen=True, slots=True)
class _After:
    """A result whose reply waits behind the audio frames queued on `handle`, if any."""

    result: JsonObject
    handle: int | None


type _Method = Callable[[JsonObject], Awaitable[JsonObject | _After]]


@dataclass(frozen=True, slots=True)
class _Request:
    id: str | int
    method: str
    params: JsonObject


class AgentLink:
    """One `/agent` WebSocket, and the sink of the Session it claims."""

    def __init__(self, websocket: WebSocket, core: Core, timing: Timing) -> None:
        self._ws = websocket
        self._core = core
        self._timing = timing
        self._session: Session | None = None
        self._control: deque[str | bytes] = deque()
        """The control lane: JSON messages waiting for the socket, ahead of any audio."""
        self._lanes: dict[int, deque[str | bytes]] = {}
        """The audio lane: each stream's frames waiting for the socket, as the streams take
        turns. An ended stream's frames are followed by its `stream.ended`."""
        self._audio_frames = max(1, timing.agent_audio_ms // 10)
        """How many frames the audio lane holds per stream before it skips the oldest."""
        self._skipped: Counter[int] = Counter()
        self._wake = asyncio.Event()
        self._close_code: int | None = None
        """Once set, the writer closes the link after the control lane is sent."""
        self._requests: set[asyncio.Task[None]] = set()
        self._stops: dict[str | int, asyncio.Event] = {}
        """What `cancel` sets to stop each request that can be, by request id."""
        self._methods: dict[str, tuple[Schema, _Method]] = {
            "devices": (_NO_PARAMS, self._devices),
            "voices": (_NO_PARAMS, self._voices),
            "command": (_COMMAND, self._command),
            "led.release": (_DEVICE, self._led_release),
            "stream.start": (_STREAM_START, self._stream_start),
            "stream.stop": (_HANDLE, self._stream_stop),
            "stream.drain": (_HANDLE, self._stream_drain),
            "say": (_SAY, self._say),
            "say.open": (_SAY_OPEN, self._say_open),
            "say.append": (_SAY_APPEND, self._say_append),
            "say.close": (_HANDLE, self._say_close),
            "listen": (_LISTEN, self._listen),
            "cancel": (_CANCEL, self._cancel),
        }

    async def run(self) -> None:
        await self._ws.accept()
        writer = asyncio.create_task(self._write())
        try:
            await self._read()
        finally:
            for task in self._requests:
                task.cancel()
            await asyncio.gather(*self._requests, return_exceptions=True)
            if self._session is not None:
                await self._core.close_session(self._session)
            self._finish(_NORMAL)
            await writer

    # SessionSink.

    def notify(self, method: str, params: JsonObject, after: int | None = None) -> None:
        message: JsonObject = {"jsonrpc": "2.0", "method": method, "params": params}
        if method == "session.ended":
            # Nothing follows it: the streams' last frames and ends go first.
            for lane in self._lanes.values():
                self._control.extend(lane)
            self._lanes.clear()
        self._send(message, after)

    def ended(self, reason: EndReason) -> None:
        self._finish(_GOING_AWAY if reason == "shutdown" else _NORMAL)

    def audio(self, handle: int, index: int, pcm: bytes) -> None:
        if self._close_code is not None:
            return
        lane = self._lanes.get(handle)
        if lane is None:
            lane = self._lanes[handle] = deque(maxlen=self._audio_frames)
        elif len(lane) == lane.maxlen:
            self._skipped[handle] += 1  # the append drops the oldest
        lane.append(encode_hub_audio(HubAudio(handle, index, pcm)))
        self._wake.set()

    def audio_ended(self, handle: int) -> int:
        if (lane := self._lanes.get(handle)) is not None:
            self._lanes[handle] = deque(lane)  # uncapped: its last frames are no longer skipped
        return self._skipped.pop(handle, 0)

    # Reading.

    async def _read(self) -> None:
        loop = asyncio.get_running_loop()
        hello_by = loop.time() + self._timing.hello_timeout_ms / 1000
        while self._close_code is None:
            try:
                async with asyncio.timeout_at(hello_by if self._session is None else None):
                    message = await self._ws.receive()
            except TimeoutError:
                _log.info("agent link: no hello in time")
                self._finish(_POLICY)
                return
            if message["type"] == "websocket.disconnect" or self._close_code is not None:
                return  # gone, or closing: a late request mustn't run for a Session that ended
            if (text := message.get("text")) is not None:
                await self._text(cast(str, text))
            elif (data := message.get("bytes")) is not None and self._session is not None:
                self._audio(cast(bytes, data))

    async def _text(self, text: str) -> None:
        request = self._parse(text)
        if request is None:
            pass
        elif self._session is None:
            await self._hello(request)
        elif request.method == "hello":
            self._error(request.id, _HUB_ERROR, "invalid_args", "this link holds a Session")
        elif request.method not in self._methods:
            message = f"no method {request.method!r}"
            self._error(request.id, _METHOD_NOT_FOUND, "unsupported", message)
        else:
            schema, run = self._methods[request.method]
            try:
                validate(schema, request.params, "params")
            except SchemaError as e:
                self._error(request.id, _INVALID_PARAMS, "invalid_args", str(e))
                return
            stop = None
            if request.method == "listen":
                # Stopped by a `cancel` naming its id.
                stop = self._stops[request.id] = asyncio.Event()
                run = functools.partial(self._listen, stop=stop)
            task = asyncio.create_task(self._request(request.id, run, request.params))
            self._requests.add(task)
            task.add_done_callback(self._requests.discard)
            if stop is not None:
                task.add_done_callback(lambda _: self._forget_stop(request.id, stop))

    def _audio(self, data: bytes) -> None:
        assert self._session is not None
        try:
            audio = decode_agent_audio(data)
        except WireError as e:
            _log.debug("agent audio dropped: %s", e)
            return
        self._core.stream_feed(self._session, audio.handle, audio.pcm)

    def _parse(self, text: str) -> _Request | None:
        """A request, or None for a notification or garbage, which gets its error here."""
        try:
            message: object = json.loads(text)
        except ValueError as e:
            self._error(None, _PARSE_ERROR, "invalid_args", f"invalid JSON: {e}")
            return None
        if isinstance(message, dict):
            message = cast(JsonObject, message)
            if "id" not in message and "method" in message:
                return None  # a notification: the hub ignores them
            request_id, method = message.get("id"), message.get("method")
            params: object = message.get("params", {})
            if (
                message.get("jsonrpc") == "2.0"
                and isinstance(request_id, str | int)
                and not isinstance(request_id, bool)
                and isinstance(method, str)
            ):
                if isinstance(params, dict):
                    return _Request(request_id, method, cast(JsonObject, params))
                self._error(request_id, _INVALID_PARAMS, "invalid_args", "params must be an object")
                return None
        self._error(None, _INVALID_REQUEST, "invalid_args", "not a JSON-RPC 2.0 request")
        return None

    async def _hello(self, request: _Request) -> None:
        """The link's first request. It must claim a Session; anything else ends the link."""
        if request.method != "hello":
            self._refuse(request.id, _HUB_ERROR, "unauthorized", "the first request must be hello")
            return
        try:
            validate(_HELLO, request.params, "params")
        except SchemaError as e:
            self._refuse(request.id, _INVALID_PARAMS, "invalid_args", str(e))
            return
        params = request.params
        try:
            self._session = await self._core.open_session(
                params["token"],
                "agent_api",
                self,
                takeover=params.get("takeover", False),
                utterances=UtteranceOptIn(**params.get("utterances", {})),
            )
        except HubError as e:
            self._refuse(request.id, _HUB_ERROR, e.code, e.message, e.data)
            return
        self._reply(request.id, {"session": self._session.id})

    def _refuse(
        self,
        request_id: str | int,
        code: int,
        hub_code: HubErrorCode,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Refuse `hello`: reply with the error, then close the link."""
        self._error(request_id, code, hub_code, message, data)
        self._finish(_POLICY)

    async def _request(self, request_id: object, run: _Method, params: JsonObject) -> None:
        try:
            result = await run(params)
        except HubError as e:
            self._error(request_id, _HUB_ERROR, e.code, e.message, e.data)
        except Exception:
            _log.exception("agent request failed")
            self._error(request_id, _INTERNAL_ERROR, "failed", "failed; see the hub log")
        else:
            if isinstance(result, _After):
                self._reply(request_id, result.result, result.handle)
            else:
                self._reply(request_id, result)

    # Methods.

    async def _devices(self, params: JsonObject) -> JsonObject:
        return {"devices": self._core.devices()}

    async def _voices(self, params: JsonObject) -> JsonObject:
        return {"voices": self._core.voices()}

    async def _command(self, params: JsonObject) -> JsonObject:
        assert self._session is not None
        return await self._core.command(
            self._session, params["address"], params["name"], params.get("args", {})
        )

    async def _led_release(self, params: JsonObject) -> JsonObject:
        assert self._session is not None
        await self._core.led_release(self._session, params["device"])
        return {}

    async def _stream_start(self, params: JsonObject) -> JsonObject:
        assert self._session is not None
        handle = await self._core.stream_start(
            self._session, params["address"], params.get("rate"), params.get("prefill_ms")
        )
        return {"handle": handle}

    async def _stream_stop(self, params: JsonObject) -> JsonObject:
        assert self._session is not None
        return await self._core.stream_stop(self._session, params["handle"])

    async def _stream_drain(self, params: JsonObject) -> JsonObject:
        assert self._session is not None
        return await self._core.stream_drain(self._session, params["handle"])

    async def _say(self, params: JsonObject) -> JsonObject:
        assert self._session is not None
        return await self._core.say(
            self._session,
            params["address"],
            params["text"],
            voice=params.get("voice"),
            lang=params.get("lang"),
            replace=params.get("replace", False),
            then_listen=self._then_listen(params),
        )

    async def _say_open(self, params: JsonObject) -> JsonObject:
        assert self._session is not None
        handle = self._core.say_open(
            self._session,
            params["address"],
            voice=params.get("voice"),
            lang=params.get("lang"),
            replace=params.get("replace", False),
            then_listen=self._then_listen(params),
        )
        return {"handle": handle}

    async def _say_append(self, params: JsonObject) -> JsonObject:
        # Requests run as tasks, in the order they came in: an append takes effect before
        # the next request starts, since nothing here awaits.
        assert self._session is not None
        self._core.say_append(self._session, params["handle"], params["text"])
        return {}

    async def _say_close(self, params: JsonObject) -> JsonObject:
        assert self._session is not None
        return await self._core.say_close(self._session, params["handle"])

    async def _listen(self, params: JsonObject, stop: asyncio.Event | None = None) -> _After:
        assert self._session is not None
        result, handle = await self._core.listen(
            self._session,
            params["address"],
            lang=params.get("lang"),
            utterances=self._utterances(params),
            stop=stop,
        )
        # Like `utterance.end`, after the Utterance's last frames.
        return _After(result, handle)

    async def _cancel(self, params: JsonObject) -> JsonObject:
        # Only a `listen` stops, for now.
        if (stop := self._stops.get(params["id"])) is not None:
            stop.set()
        return {}

    def _utterances(self, params: JsonObject) -> UtteranceOptIn:
        """The Session's Utterance opt-in, as `params` override it."""
        assert self._session is not None
        return replace(self._session.utterances, **params.get("utterances", {}))

    def _then_listen(self, params: JsonObject) -> ThenListen | None:
        then: bool | JsonObject = params.get("then_listen", False)
        if then is False:
            return None
        options: JsonObject = {} if then is True else then
        return ThenListen(
            raw=options.get("raw", False),
            lang=options.get("lang"),
            utterances=self._utterances(options),
        )

    def _forget_stop(self, request_id: str | int, stop: asyncio.Event) -> None:
        if self._stops.get(request_id) is stop:
            del self._stops[request_id]

    # Writing.

    def _reply(self, request_id: object, result: JsonObject, after: int | None = None) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result}, after)

    def _error(
        self,
        request_id: object,
        code: int,
        hub_code: HubErrorCode,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        error = {"code": code, "message": message, "data": {"code": hub_code} | (data or {})}
        self._send({"jsonrpc": "2.0", "id": request_id, "error": error})

    def _send(self, message: JsonObject, after: int | None = None) -> None:
        """Queue `message` on the control lane; or, `after` a handle, behind the audio frames
        of it queued so far, which nothing else waits behind."""
        if self._close_code is not None:
            return
        if after is not None and (lane := self._lanes.get(after)):
            lane.append(_encode(message))
        else:
            self._control.append(_encode(message))
            self._wake.set()

    def _finish(self, code: int) -> None:
        """Close the link once the control lane has gone out. Nothing more is queued, and the
        audio lane is dropped."""
        if self._close_code is None:
            self._close_code = code
            self._lanes.clear()
            self._wake.set()

    def _next(self) -> str | bytes | None:
        """What the socket takes next: the control lane first, then the audio lane's next
        message, from the stream whose turn it is."""
        if self._control:
            return self._control.popleft()
        for handle, lane in self._lanes.items():
            message = lane.popleft()
            del self._lanes[handle]
            if lane:
                self._lanes[handle] = lane  # to the back of the turns
            return message
        return None

    async def _write(self) -> None:
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                while (message := self._next()) is not None:
                    if isinstance(message, str):
                        await self._ws.send_text(message)
                    else:
                        await self._ws.send_bytes(message)
                if self._close_code is not None:
                    await self._ws.close(self._close_code)
                    return
        except WebSocketDisconnect, RuntimeError:
            pass  # the agent is gone


def _encode(message: JsonObject) -> str:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False)
