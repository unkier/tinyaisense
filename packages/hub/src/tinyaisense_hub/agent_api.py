"""The agent API front-end: JSON-RPC 2.0 over the `/agent` WebSocket.

docs/protocol/agent.md is the contract. This module only translates between it and the core
facade: it parses and checks requests, calls the core, and writes replies and notifications.
"""

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from tinyaisense_wire import JsonObject, Schema, SchemaError, validate

from .config import Timing
from .core import Core
from .errors import HubError, HubErrorCode
from .session import EndReason, Session, UtteranceOptIn

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

_RATES = [16000, 24000, 48000]
_HELLO: Schema = {
    "type": "object",
    "required": ["token"],
    "additionalProperties": False,
    "properties": {
        "token": {"type": "string"},
        "takeover": {"type": "boolean"},
        "utterances": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "transcript": {"type": "boolean"},
                "audio": {"type": "boolean"},
                "rate": {"type": "integer", "enum": _RATES},
            },
        },
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

_log = logging.getLogger("tinyaisense_hub.agent_api")


def agent_app(core: Core, timing: Timing) -> Starlette:
    async def agent(websocket: WebSocket) -> None:
        await AgentLink(websocket, core, timing).run()

    return Starlette(routes=[WebSocketRoute("/agent", agent)])


type _Method = Callable[[JsonObject], Awaitable[JsonObject]]


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
        self._control: deque[str] = deque()
        """JSON messages waiting for the socket."""
        self._wake = asyncio.Event()
        self._close_code: int | None = None
        """Once set, the writer closes the link after the lane is sent."""
        self._requests: set[asyncio.Task[None]] = set()
        self._methods: dict[str, tuple[Schema, _Method]] = {
            "devices": (_NO_PARAMS, self._devices),
            "command": (_COMMAND, self._command),
            "led.release": (_DEVICE, self._led_release),
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

    def notify(self, method: str, params: JsonObject) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def ended(self, reason: EndReason) -> None:
        self._finish(_GOING_AWAY if reason == "shutdown" else _NORMAL)

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
            task = asyncio.create_task(self._request(request.id, run, request.params))
            self._requests.add(task)
            task.add_done_callback(self._requests.discard)

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
            self._reply(request_id, result)

    # Methods.

    async def _devices(self, params: JsonObject) -> JsonObject:
        return {"devices": self._core.devices()}

    async def _command(self, params: JsonObject) -> JsonObject:
        assert self._session is not None
        return await self._core.command(
            self._session, params["address"], params["name"], params.get("args", {})
        )

    async def _led_release(self, params: JsonObject) -> JsonObject:
        assert self._session is not None
        await self._core.led_release(self._session, params["device"])
        return {}

    # Writing.

    def _reply(self, request_id: object, result: JsonObject) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

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

    def _send(self, message: JsonObject) -> None:
        if self._close_code is None:
            self._control.append(json.dumps(message, separators=(",", ":"), ensure_ascii=False))
            self._wake.set()

    def _finish(self, code: int) -> None:
        """Close the link once what's queued has gone out. Nothing more is queued."""
        if self._close_code is None:
            self._close_code = code
            self._wake.set()

    async def _write(self) -> None:
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                while self._control:
                    await self._ws.send_text(self._control.popleft())
                if self._close_code is not None:
                    await self._ws.close(self._close_code)
                    return
        except WebSocketDisconnect, RuntimeError:
            pass  # the agent is gone
