"""The MCP front-end: tools over Streamable HTTP at `/mcp`, a sibling of the agent API.

It runs on the MCP SDK's Streamable HTTP session manager, the one `streamable_http_app()`
builds, with SSE responses; the hub's HTTP lifespan runs the manager. Like the agent API, it
translates between MCP tool calls and the core facade; and it gives MCP, which can't push, what
it can't do without: calls that return in time, and an Event queue (`mcp_events`).
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any, cast

import mcp.types as types
from mcp.server import Server, ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send
from tinyaisense_wire import KINDS, X_HUB_SESSION, JsonObject, Schema, SchemaError, validate

from .config import Timing
from .core import Core
from .errors import HubError, HubErrorCode
from .mcp_events import Follower, Inbox, Joined
from .session import Session, ThenListen, UtteranceOptIn

_VOICE_LOOP = """\
tinyaisense lets you hear and speak to a human through small devices near them, e.g. an \
M5Stack Atom Echo: a mic, a speaker, a button and an LED. The voice loop: `listen` returns \
what the human said, and `say` answers aloud, returning once it has played. To ask and hear \
the answer in one call, `say` with `then_listen`. Speak plainly and briefly: no markdown, \
lists or code. Devices are half-duplex: they don't hear while they speak."""
_EVENTS = """\
Events: any tool result, errors included, may carry `events`: what happened since your last \
call, e.g. what the human said by push-to-talk, a button gesture, or a device going online or \
offline. Each comes once: act on it. With nothing to do, `wait_for_event` waits for the next. \
A call still running after {block} returns `pending`; its result comes later as an event."""
_JOINED_PENDING = """\
A call still running after {block} returns `pending`; its result comes later, through the \
program that runs you."""
_BARGE_IN = """\
Barge-in: the human can press the button to cut you off. `say` then returns \
`interrupted: true`, `heard_text`: what they heard before the cut, and `utterance`: what they \
said while holding the button, push-to-talk; a short click means "stop talking". \
`set_reflex` with `armed: false` makes a press only an event."""
_ADDRESSES = """\
Addresses name capabilities `<device>/<capability>`, e.g. `kitchen/speaker`: `devices` lists \
them, with each one's commands. Leave `address` out when only one online device has what the \
tool needs. `command` drives the rest, e.g. the LED's `set` and `off`; the LED then shows your \
colours instead of the device's status, until `led_release`."""
_CLAIM = """\
One agent holds the hub at a time. Your first call other than `devices` and `voices` claims \
it, and it's released after {idle} with no call. `busy` says who holds it and since when."""

INSTRUCTIONS = "\n\n".join((_VOICE_LOOP, _EVENTS, _BARGE_IN, _ADDRESSES, _CLAIM)) + "\n"
"""The server instructions, given the idle time and the longest a call blocks. The first
paragraph must stand alone, in 512 chars; the whole stays under 2048."""
JOINED_INSTRUCTIONS = "\n\n".join((_VOICE_LOOP, _BARGE_IN, _ADDRESSES, _JOINED_PENDING)) + "\n"
"""The server instructions for an MCP link that joined an agent-API Session, given the longest
a call blocks: it has no Event queue, and the Session is the agent-API link's."""

_ADDRESS = {
    "type": "string",
    "description": "`<device>/<capability>`, e.g. `kitchen/speaker`; left out, the one "
    "online capability of the Kind the tool needs",
}
_DEVICE = {"type": "string", "description": "a device name, e.g. `kitchen`"}
_LANG = {"type": "string", "description": "a language code, e.g. `en` or `ru`"}
_NO_ARGS: Schema = {"type": "object", "properties": {}, "additionalProperties": False}
_OBJECT: Schema = {"type": "object", "properties": {}}

_SAY_RESULT: Schema = {
    "type": "object",
    "required": ["reason", "played_ms", "interrupted", "heard_text"],
    "properties": {
        "reason": {
            "type": "string",
            "enum": ["drained", "stopped", "preempted", "reflex", "disconnected"],
        },
        "played_ms": {"type": "integer"},
        "interrupted": {"type": "boolean"},
        "heard_text": {"type": "string"},
        "degraded": {"type": "boolean"},
        "utterance": {
            "type": "object",
            "required": ["text", "reason"],
            "properties": {
                "text": {"type": "string"},
                "lang": {"type": "string"},
                "reason": {"type": "string"},
            },
            "description": "with `then_listen`, or cut off by the button: what the human said then",
        },
    },
}
_UTTERANCE_END: Schema = {
    "type": "object",
    "required": ["utterance_id", "reason", "stats"],
    "properties": {
        "utterance_id": {"type": "integer"},
        "reason": {
            "type": "string",
            "enum": [
                "stopped",
                "end_of_turn",
                "no_speech",
                "max_duration",
                "button",
                "click",
                "disconnected",
            ],
        },
        "transcript": {"type": "string"},
        "lang": {"type": "string"},
        "degraded": {"type": "boolean"},
        "stats": {
            "type": "object",
            "properties": {"duration_ms": {"type": "integer"}, "lost": {"type": "integer"}},
        },
    },
}
_DEVICES_RESULT: Schema = {
    "type": "object",
    "required": ["devices"],
    "properties": {
        "devices": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "online", "board", "capabilities", "exclusive"],
                "properties": {
                    "name": {"type": "string"},
                    "online": {"type": "boolean"},
                    "board": {"type": "string"},
                    "capabilities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["address", "kind", "commands"],
                            "properties": {
                                "address": {"type": "string"},
                                "kind": {"type": "string"},
                                "params": {"type": "object"},
                                "commands": {
                                    "type": "object",
                                    "description": "each command's args, as a JSON Schema",
                                },
                            },
                        },
                    },
                    "exclusive": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                        "description": "capabilities of which only one runs at a time",
                    },
                },
            },
        }
    },
}
_VOICES_RESULT: Schema = {
    "type": "object",
    "required": ["voices"],
    "properties": {
        "voices": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "lang", "name"],
                "properties": {
                    "id": {"type": "string"},
                    "lang": {"type": "string"},
                    "name": {"type": "string"},
                },
            },
        }
    },
}

_EVENTS_RESULT: Schema = {"type": "object", "required": ["events"], "properties": {}}

_EVENT: Schema = {
    "type": "object",
    "required": ["event", "t"],
    "properties": {
        "event": {
            "type": "string",
            "description": "`utterance`, `result`, `device.online`, `button.click`, ...",
        },
        "t": {"type": "integer", "description": "when, in epoch ms"},
    },
}
_DELIVERY = {
    "events": {
        "type": "array",
        "items": _EVENT,
        "description": "what happened since your last call, each Event once",
    },
    "dropped": {
        "type": "integer",
        "description": "how many Events were dropped, the oldest first, for want of room",
    },
}


_PENDING = {
    "type": "integer",
    "description": "the call is still running: its result comes as the `result` Event with "
    "this `pending`",
}


def _delivering(schema: Schema) -> dict[str, Any]:
    """The output schema of a tool whose result `schema` says, and that also carries the
    Events queued for its caller."""
    return {**schema, "properties": {**schema["properties"], **_DELIVERY}}


def _blocking(schema: Schema) -> dict[str, Any]:
    """The output schema of a tool whose result `schema` says, unless it's still running:
    then it's `pending`, and the result comes later, as an Event."""
    blocking = _delivering(schema)
    blocking["properties"]["pending"] = _PENDING
    if (required := blocking.pop("required", None)) is not None:
        blocking["anyOf"] = [{"required": required}, {"required": ["pending"]}]
    return blocking


_READ_ONLY = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
_ACTS = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

TOOLS = (
    types.Tool(
        name="devices",
        description="The paired devices, whether each is online, and their capabilities: each "
        "one's Address, Kind and commands.",
        input_schema=dict(_NO_ARGS),
        output_schema=_delivering(_DEVICES_RESULT),
        annotations=_READ_ONLY,
    ),
    types.Tool(
        name="voices",
        description="The Voices `say` can speak in, by id, with their language.",
        input_schema=dict(_NO_ARGS),
        output_schema=_delivering(_VOICES_RESULT),
        annotations=_READ_ONLY,
    ),
    types.Tool(
        name="say",
        description="Speak `text` aloud on a speaker. Returns once it has played, or was cut "
        "off: then `interrupted` is true, `heard_text` is what the human heard, and "
        "`utterance` what they said if they cut it off with the button. Queued after what's "
        "already speaking, unless `replace`.",
        input_schema={
            "type": "object",
            "required": ["text"],
            "additionalProperties": False,
            "properties": {
                "text": {"type": "string", "description": "plain words to speak"},
                "address": _ADDRESS,
                "voice": {"type": "string", "description": "a Voice id from `voices`"},
                "lang": _LANG,
                "replace": {
                    "type": "boolean",
                    "description": "cut off what's speaking or queued, instead of waiting",
                },
                "then_listen": {
                    "type": "boolean",
                    "description": "once it has played, listen for the human's answer, and "
                    "return it as `utterance`",
                },
            },
        },
        output_schema=_blocking(_SAY_RESULT),
        annotations=_ACTS,
    ),
    types.Tool(
        name="listen",
        description="Listen on a mic until the human has had their say. Returns the final "
        "transcript, its `lang`, and why it ended.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"address": _ADDRESS, "lang": _LANG},
        },
        output_schema=_blocking(_UTTERANCE_END),
        annotations=_ACTS,
    ),
    types.Tool(
        name="wait_for_event",
        description="Wait until an Event comes, e.g. the human speaking by push-to-talk, and "
        "return the Events: at once if some are waiting.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "timeout_s": {
                    "type": "number",
                    "minimum": 0,
                    "description": "how long to wait at most, in seconds",
                }
            },
        },
        output_schema=_delivering(_EVENTS_RESULT),
        annotations=_READ_ONLY,
    ),
    types.Tool(
        name="command",
        description="Send a command to a capability, e.g. an LED's `set` with "
        '`{"color": "#00ff00"}`, or `off`. `devices` lists each capability\'s commands.',
        input_schema={
            "type": "object",
            "required": ["address", "name", "args"],
            "additionalProperties": False,
            "properties": {
                "address": {"type": "string", "description": "`<device>/<capability>`"},
                "name": {"type": "string", "description": "the command, e.g. `set`"},
                "args": {"type": "object", "description": "the command's args"},
            },
        },
        output_schema=_blocking(_OBJECT),
        annotations=_ACTS,
    ),
    types.Tool(
        name="led_release",
        description="Hand a device's LEDs back to its status display.",
        input_schema={
            "type": "object",
            "required": ["device"],
            "additionalProperties": False,
            "properties": {"device": _DEVICE},
        },
        output_schema=_blocking(_OBJECT),
        annotations=_ACTS,
    ),
    types.Tool(
        name="set_reflex",
        description="Arm or disarm a device's button. Armed, a press cuts off the speaker and "
        "records the human; disarmed, it's only an event.",
        input_schema={
            "type": "object",
            "required": ["device", "armed"],
            "additionalProperties": False,
            "properties": {"device": _DEVICE, "armed": {"type": "boolean"}},
        },
        output_schema=_blocking(_OBJECT),
        annotations=_ACTS,
    ),
)

_TRANSCRIPT = UtteranceOptIn(transcript=True, audio=False)
"""What an Utterance that MCP hears delivers, whatever its Session's opt-in: MCP takes no
audio."""
_QUEUE_TOOLS = ("wait_for_event",)
"""The tools of the Event queue, which MCP links that joined an agent-API Session don't have."""

_log = logging.getLogger("tinyaisense_hub.mcp")
_WEB = ("http", "https")

type _Tool = Callable[[Session, JsonObject, asyncio.Event], Awaitable[JsonObject]]
"""A tool that runs in the caller's Session. Setting the event stops it: the client cancelled
the call."""
type _Outcome = tuple[JsonObject, bool]
"""A tool call's result, and whether it's an error."""


class McpServer:
    """The `/mcp` endpoint."""

    def __init__(self, core: Core, allowed_hosts: Sequence[str], timing: Timing) -> None:
        self._core = core
        self._timing = timing
        block = _duration(timing.mcp_max_block_ms)
        server = Server[Any](
            "tinyaisense",
            version=version("tinyaisense-hub"),
            instructions=INSTRUCTIONS.format(idle=_duration(timing.mcp_idle_ms), block=block),
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )
        server.middleware.append(_Instructions(JOINED_INSTRUCTIONS.format(block=block)))
        security = TransportSecuritySettings(
            allowed_hosts=list(allowed_hosts),
            allowed_origins=[f"{scheme}://{host}" for host in allowed_hosts for scheme in _WEB],
        )
        # What `server.streamable_http_app()` builds, but as one route of the hub's app, so that
        # `/agent` shares the port, behind the agent token rather than the SDK's OAuth.
        self._manager = StreamableHTTPSessionManager(server, security_settings=security)
        self.route = Route("/mcp", _Endpoint(StreamableHTTPASGIApp(self._manager), core))
        self._sessionless: dict[str, Callable[[JsonObject], Awaitable[JsonObject]]] = {
            "devices": self._devices,
            "voices": self._voices,
        }
        """The tools that need no Session."""
        self._tools: dict[str, _Tool] = {
            "say": self._say,
            "listen": self._listen,
            "command": self._command,
            "led_release": self._led_release,
            "set_reflex": self._set_reflex,
            "wait_for_event": self._wait_for_event,
        }
        """The tools that claim the hub."""
        self._schemas = {tool.name: tool.input_schema for tool in TOOLS}
        self._calls: set[asyncio.Task[_Outcome]] = set()
        """The calls running, those that returned `pending` or were cancelled too: kept from
        the garbage collector."""

    @asynccontextmanager
    async def run(self, _app: object = None) -> AsyncGenerator[None]:
        """Serve MCP sessions: the HTTP server's lifespan."""
        async with self._manager.run():
            yield

    async def _list_tools(
        self, ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        if _caller(ctx).hub_session is None:
            return types.ListToolsResult(tools=list(TOOLS))
        tools = [tool for tool in TOOLS if tool.name not in _QUEUE_TOOLS]
        return types.ListToolsResult(tools=tools)

    async def _call_tool(
        self, ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        name, args = params.name, params.arguments or {}
        caller = _caller(ctx)
        if name not in self._schemas or (caller.hub_session is not None and name in _QUEUE_TOOLS):
            raise MCPError(types.INVALID_PARAMS, f"Unknown tool: {name}")
        try:
            validate(self._schemas[name], args, "arguments")
        except SchemaError as e:
            return self._result(caller, (_error("invalid_args", str(e)), True))
        if (sessionless := self._sessionless.get(name)) is not None:
            outcome = await _outcome(name, self._sessionless_call(caller, sessionless, args))
        else:
            outcome = await self._blocking(ctx, caller, name, args)
        return self._result(caller, outcome)

    async def _sessionless_call(
        self,
        caller: _Caller,
        tool: Callable[[JsonObject], Awaitable[JsonObject]],
        args: JsonObject,
    ) -> JsonObject:
        """Run a tool that needs no Session; on a link that joined one, only while it lasts."""
        if caller.hub_session is not None:
            self._core.joined(caller.hub_session)
        return await tool(args)

    async def _blocking(
        self, ctx: ServerRequestContext[Any], caller: _Caller, name: str, args: JsonObject
    ) -> _Outcome:
        """Run a tool that claims the hub, for `mcp_max_block_ms` at most: `wait_for_event`
        returns by then by itself. What's left of it runs on, and its result comes as an
        Event. A client cancel stops it."""
        call = _Call(self._core, caller, name, self._tools[name], args)
        self._calls.add(call.task)
        call.task.add_done_callback(self._calls.discard)
        start = asyncio.get_running_loop().time()
        deadline = (
            None if name == "wait_for_event" else start + self._timing.mcp_max_block_ms / 1000
        )
        try:
            done = await self._wait(ctx, call.task, start, deadline)
            if not done and not call.claimed.done():  # it can't be pending till it has claimed
                await self._wait(ctx, call.claimed, start, None)
                done = call.task.done()
        except BaseException:  # cancelled, or the client has gone
            call.stop.set()
            raise
        if done:
            return call.task.result()
        return {"pending": call.leftover()}, False

    async def _wait(
        self,
        ctx: ServerRequestContext[Any],
        future: asyncio.Future[Any],
        start: float,
        deadline: float | None,
    ) -> bool:
        """Wait until `future` is done, or the loop time is `deadline`, with progress every
        `mcp_progress_ms`, if the client asked for it: the seconds since `start`. Returns
        whether it's done."""
        loop = asyncio.get_running_loop()
        every = self._timing.mcp_progress_ms / 1000
        while True:
            wait = every if deadline is None else min(every, deadline - loop.time())
            if (await asyncio.wait((future,), timeout=max(0, wait)))[0]:
                return True
            if deadline is not None and loop.time() >= deadline:
                return False
            await ctx.session.report_progress(round(loop.time() - start, 3))

    def _result(self, caller: _Caller, outcome: _Outcome) -> types.CallToolResult:
        """A tool result, with the Events queued for the `caller`, if its MCP-only Session
        holds the hub."""
        result, error = outcome
        if (
            caller.hub_session is None
            and (session := self._core.mcp_session(caller.identity)) is not None
        ):
            result = result | _inbox(session).take()
        return _result(result, error=error)

    # The tools.

    async def _devices(self, args: JsonObject) -> JsonObject:
        return {"devices": [_device(device) for device in self._core.devices()]}

    async def _voices(self, args: JsonObject) -> JsonObject:
        return {"voices": self._core.voices()}

    async def _say(self, session: Session, args: JsonObject, stop: asyncio.Event) -> JsonObject:
        """With `then_listen`, or cut by the Reflex, it returns what the human said then."""
        then = None
        if args.get("then_listen"):
            then = ThenListen(raw=False, lang=None, utterances=_TRANSCRIPT)
        address = self._address(args, "speaker")
        device = self._core.device_of(address)
        result = await self._core.say(
            session,
            address,
            args["text"],
            voice=args.get("voice"),
            lang=args.get("lang"),
            replace=args.get("replace", False),
            then_listen=then,
            stop=stop,
        )
        follower = _follower(session)
        if (utterance_id := result.pop("utterance_id", None)) is not None:
            answer = asyncio.ensure_future(follower.answer(utterance_id))
            if not await _before(answer, stop):
                self._core.utterance_stop(session, utterance_id)
            result["utterance"] = await answer
        elif result["reason"] == "reflex":
            if (said := await follower.cut(device, stop)) is not None:
                result["utterance"] = said
        return result

    async def _listen(self, session: Session, args: JsonObject, stop: asyncio.Event) -> JsonObject:
        result, _ = await self._core.listen(
            session,
            self._address(args, "mic"),
            lang=args.get("lang"),
            utterances=_TRANSCRIPT,
            stop=stop,
        )
        return result

    async def _command(self, session: Session, args: JsonObject, stop: asyncio.Event) -> JsonObject:
        return await self._core.command(session, args["address"], args["name"], args["args"])

    async def _led_release(
        self, session: Session, args: JsonObject, stop: asyncio.Event
    ) -> JsonObject:
        await self._core.led_release(session, args["device"])
        return {}

    async def _set_reflex(
        self, session: Session, args: JsonObject, stop: asyncio.Event
    ) -> JsonObject:
        await self._core.reflex_set(session, args["device"], args["armed"])
        return {}

    async def _wait_for_event(
        self, session: Session, args: JsonObject, stop: asyncio.Event
    ) -> JsonObject:
        block = self._timing.mcp_max_block_ms / 1000
        queued = asyncio.ensure_future(_inbox(session).wait())
        try:
            async with asyncio.timeout(min(args.get("timeout_s", block), block)):
                await _before(queued, stop)
        except TimeoutError:
            pass
        finally:
            queued.cancel()
        return {"events": []}

    def _address(self, args: JsonObject, kind: str) -> str:
        """The `address` arg; or, left out, the one online capability of `kind`."""
        address: str | None = args.get("address")
        return self._core.only_online(kind) if address is None else address


class _Call:
    """A call of a tool that claims the hub, run as a task of its own: it may run on after
    the MCP request, as a leftover."""

    def __init__(
        self, core: Core, caller: _Caller, name: str, tool: _Tool, args: JsonObject
    ) -> None:
        self.stop = asyncio.Event()
        """Set when the client cancels it."""
        self.session: Session | None = None
        """The Session it runs in, once it has claimed it."""
        self.claimed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        """Done once it has claimed the hub, or failed to."""
        self._name = name
        self.task = asyncio.create_task(self._run(core, caller, tool, args))
        self.task.add_done_callback(self._claimed)

    async def _run(self, core: Core, caller: _Caller, tool: _Tool, args: JsonObject) -> _Outcome:
        async def run() -> JsonObject:
            if caller.hub_session is None:
                claim = core.mcp_call(caller.identity, Inbox)
            else:
                claim = nullcontext(core.joined(caller.hub_session))
            async with claim as session:
                if caller.hub_session is not None and session.joined is None:
                    session.joined = Joined(session)  # before it acts, to follow what it does
                self.session = session
                self._claimed()
                return await tool(session, args, self.stop)

        return await _outcome(self._name, run())

    def _claimed(self, _: object = None) -> None:
        if not self.claimed.done():
            self.claimed.set_result(None)

    def leftover(self) -> int:
        """It runs on after its request, in the Session it claimed: its result is to come as
        an Event. Returns its `pending` id."""
        assert self.session is not None
        follower = _follower(self.session)
        pending = follower.pending()

        def done(task: asyncio.Task[_Outcome]) -> None:
            if not task.cancelled():
                follower.late(pending, self._name, *task.result())

        self.task.add_done_callback(done)
        return pending


async def _before(future: asyncio.Future[Any], stop: asyncio.Event) -> bool:
    """Wait until `future` is done, or `stop` is set. Returns whether it was done first."""
    stopping = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait((future, stopping), return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopping.cancel()
    return future.done()


async def _outcome(name: str, result: Awaitable[JsonObject]) -> _Outcome:
    """What the call of tool `name` comes to: its `result`, or the error it raises."""
    try:
        return await result, False
    except HubError as e:
        return e.data | _error(e.code, e.message), True
    except Exception:
        _log.exception("MCP tool %s failed", name)
        return _error("failed", "failed; see the hub log"), True


class _Instructions:
    """Gives an MCP link that joined an agent-API Session its own server instructions."""

    def __init__(self, instructions: str) -> None:
        self._instructions = instructions

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        result = await call_next(ctx)
        if ctx.method in ("initialize", "server/discover") and _caller(ctx).hub_session is not None:
            assert isinstance(result, dict)
            result["instructions"] = self._instructions
        return result


class _Endpoint:
    """The MCP app, behind the agent token as a Bearer. A legacy `DELETE` that ends an MCP
    session also releases the Session it holds."""

    def __init__(self, app: StreamableHTTPASGIApp, core: Core) -> None:
        self._app = app
        self._core = core

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = Headers(scope=scope)
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not self._core.authorized(token.strip()):
            response = JSONResponse(
                {"error": "invalid_token", "error_description": "the agent token doesn't match"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
            await response(scope, receive, send)
            return
        statuses: list[int] = []

        async def sent(message: Message) -> None:
            if message["type"] == "http.response.start":
                statuses.append(message["status"])
            await send(message)

        await self._app(scope, receive, sent)
        mcp_session = headers.get(MCP_SESSION_ID_HEADER)
        if scope["method"] == "DELETE" and mcp_session and statuses == [200]:  # it ended
            await self._core.mcp_release(_mcp_session_identity(mcp_session))


@dataclass(frozen=True, slots=True)
class _Caller:
    """Who makes an MCP request."""

    identity: str
    """The agent: its MCP session, on the handshake versions; else the name its MCP client
    gives and its address."""
    hub_session: str | None
    """The agent-API Session its link joined, by id, if any: its `X-Hub-Session`."""


def _caller(ctx: ServerRequestContext[Any]) -> _Caller:
    request = cast(Request, ctx.request)
    hub_session = request.headers.get(X_HUB_SESSION)
    if mcp_session := request.headers.get(MCP_SESSION_ID_HEADER):
        return _Caller(_mcp_session_identity(mcp_session), hub_session)
    client = ctx.session.client_params
    name = client.client_info.name if client is not None else ""
    return _Caller(f"agent {name}@{request.client.host if request.client else ''}", hub_session)


def _inbox(session: Session) -> Inbox:
    """Where an MCP-only Session's notifications go."""
    assert isinstance(session.sink, Inbox)
    return session.sink


def _follower(session: Session) -> Follower:
    """What MCP hears of `session`: an MCP-only one's Event queue; else what its MCP links
    that joined it hear."""
    follower = session.sink if session.joined is None else session.joined
    assert isinstance(follower, Follower)
    return follower


def _mcp_session_identity(mcp_session: str) -> str:
    return f"MCP session {mcp_session}"


def _duration(ms: int) -> str:
    return f"{ms // 60_000} min" if ms % 60_000 == 0 else f"{ms / 1000:g} s"


def _device(device: JsonObject) -> JsonObject:
    """A device from the core's `devices`, as MCP lists it: its capabilities by Address, with
    the commands agents may send them."""
    name: str = device["name"]
    manifest: JsonObject = device["manifest"]
    capabilities: list[JsonObject] = []
    for capability in cast(list[JsonObject], manifest["capabilities"]):
        kind = KINDS.get(capability["kind"])
        commands = kind.commands.values() if kind is not None else ()
        listed: JsonObject = {
            "address": f"{name}/{capability['id']}",
            "kind": capability["kind"],
            "commands": {c.name: dict(c.args) for c in commands if not c.hub_only},
        }
        if "params" in capability:
            listed["params"] = capability["params"]
        capabilities.append(listed)
    return {
        "name": name,
        "online": device["online"],
        "board": manifest["board"],
        "capabilities": capabilities,
        "exclusive": [
            [f"{name}/{cap_id}" for cap_id in group]
            for group in cast(list[list[str]], manifest["exclusive"])
        ],
    }


def _error(code: HubErrorCode, message: str) -> JsonObject:
    return {"code": code, "message": message}


def _result(result: JsonObject, *, error: bool = False) -> types.CallToolResult:
    """A tool result: structured, and the same JSON as text."""
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return types.CallToolResult(
        content=[types.TextContent(text=text)], structured_content=result, is_error=error
    )
