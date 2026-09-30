"""`tinyaisense mcp`: the hub's MCP tools over stdio, for MCP clients that can't reach `/mcp`
over Streamable HTTP.

The shim holds a Session over the agent API, and forwards each MCP call to `/mcp`, joined to
that Session with `X-Hub-Session`. With `channel`, it pushes what the human says by
push-to-talk and button gestures into Claude Code, as channel events, so that they wake it
while it waits.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from collections.abc import AsyncGenerator, Awaitable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

import anyio
import mcp.types as types
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server, ServerRequestContext
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.message import SessionMessage
from tinyaisense_wire import X_HUB_SESSION, JsonObject
from websockets.exceptions import InvalidHandshake

from . import __version__
from .session import ApiError, Notification, Session, connect

_LINE_LIMIT = 1 << 24
"""The longest JSON-RPC message, in bytes, that the shim reads from stdin."""

_CHANNEL = "claude/channel"
"""Claude Code's channel capability; its events are `notifications/claude/channel`."""
_CHANNEL_INSTRUCTIONS = """\
Events come as <channel> tags, even while you're idle: an `utterance`, what the human said by \
push-to-talk, as its text; a button gesture, e.g. `button.click`; or the `result` of a call \
that returned `pending`. Act on each, e.g. answer an utterance with `say`."""
_GESTURES = ("button.click", "button.double_click", "button.long_press")
_CUT_RETURN_S = 1.0
"""How long a push-to-talk Utterance's channel event waits at most, once it has ended, for a
`say` it cut to return it; and how long the `say`'s result stands for it."""

_Read = MemoryObjectReceiveStream[SessionMessage | Exception]
_Write = MemoryObjectSendStream[SessionMessage]


class ShimError(Exception):
    """Why the shim can't serve, or stopped: for the human who runs it."""


async def run(hub: str, token: str, *, takeover: bool, channel: bool) -> None:
    """Serve MCP over stdio until stdin closes. Raises `ShimError` if the shim can't, or once
    its Session ends."""
    agent_url, mcp_url = _urls(hub)
    try:
        session = await connect(agent_url, token, takeover=takeover)
    except ApiError as e:
        raise ShimError(_refusal(e)) from None
    except (OSError, InvalidHandshake) as e:
        raise ShimError(f"can't reach the hub at {hub}: {e}") from None
    async with session, contextlib.AsyncExitStack() as stack:
        headers = {"Authorization": f"Bearer {token}", X_HUB_SESSION: session.id}
        http = await stack.enter_async_context(create_mcp_http_client(headers))
        transport = streamable_http_client(mcp_url, http_client=http)
        info = types.Implementation(name="tinyaisense mcp", version=__version__)
        try:
            client = await stack.enter_async_context(Client(transport, client_info=info))
        except Exception as e:
            host = urlsplit(hub).netloc
            raise ShimError(
                f"the hub's MCP server refused the shim: {_cause(e)}; is {host} in the hub's "
                "allowed_hosts?"
            ) from None
        served = await _Shim(session, client, _Channel() if channel else None).serve()
    if not served:  # out of the MCP client's task group, which would wrap it
        raise ShimError(f"the Session ended: {session.ended}")


class _Shim:
    """An MCP server over stdio, whose tools are the hub's, called in `session` by `client`."""

    def __init__(self, session: Session, client: Client, channel: _Channel | None) -> None:
        self._session = session
        self._client = client
        self._channel = channel
        self._pushes: _Write | None = None
        """Where channel events go: to the MCP client."""
        self._early: list[_Event] | None = []
        """The channel events from before the MCP client initialized, to push once it has;
        None since."""

    async def serve(self) -> bool:
        """Serve MCP over stdio until stdin closes, or the Session ends. Returns whether
        stdin closed first."""
        instructions = self._client.instructions
        experimental: dict[str, dict[str, Any]] = {}
        if self._channel is not None:
            instructions = f"{instructions or ''}\n\n{_CHANNEL_INSTRUCTIONS}".lstrip()
            experimental[_CHANNEL] = {}
        server = Server[Any](
            "tinyaisense",
            version=__version__,
            instructions=instructions,
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )
        server.add_notification_handler(
            "notifications/initialized", types.NotificationParams, self._on_initialized
        )
        options = server.create_initialization_options(experimental_capabilities=experimental)
        async with _stdio() as (read, write), write.clone() as self._pushes:
            serving = asyncio.ensure_future(server.run(read, write, options))
            following = asyncio.ensure_future(self._follow())
            await asyncio.wait((serving, following), return_when=asyncio.FIRST_COMPLETED)
            for task in (serving, following):
                task.cancel()
            await asyncio.gather(serving, following, return_exceptions=True)
        return self._session.ended is None

    async def _on_initialized(
        self, ctx: ServerRequestContext[Any], params: types.NotificationParams
    ) -> None:
        while self._early:  # more may come meanwhile
            await self._push(self._early.pop(0))
        self._early = None

    async def _follow(self) -> None:
        """Follow the Session's notifications until it ends, pushing its channel events to
        the MCP client."""
        async for notification in self._session.notifications():
            if self._channel is None or (event := self._channel.event(notification)) is None:
                continue
            if event.said is not None and await self._channel.returned(event.said):
                continue
            if self._early is not None:
                self._early.append(event)
            else:
                await self._push(event)

    async def _push(self, event: _Event) -> None:
        assert self._pushes is not None
        params = {"content": event.content, "meta": event.meta}
        push = types.JSONRPCNotification(
            jsonrpc="2.0", method=f"notifications/{_CHANNEL}", params=params
        )
        await self._pushes.send(SessionMessage(push))

    async def _list_tools(
        self, ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return await self._client.list_tools()

    async def _call_tool(
        self, ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        call = self._client.call_tool(params.name, params.arguments)
        if params.name == "say" and self._channel is not None:
            return await self._channel.say(call)
        return await call


class _Channel:
    """Which of the Session's notifications go to Claude Code as channel events, and as what:
    what the human says by push-to-talk, button gestures, and the results of calls that
    returned `pending`. The rest come in tool results, or aren't for an LLM."""

    def __init__(self) -> None:
        self._pressed: dict[int, str] = {}
        """The Address of each push-to-talk Utterance under way, by id."""
        self._saying = 0
        """How many `say`s run."""
        self._returns = asyncio.Event()
        """Set, and replaced, when a `say` returns."""
        self._cuts: list[tuple[float, JsonObject]] = []
        """What the human said that cut `say`s, as they returned it, and when they did."""

    async def say(self, call: Awaitable[types.CallToolResult]) -> types.CallToolResult:
        """Run a `say`, noting what it returns that the human said cutting it off."""
        self._saying += 1
        try:
            result = await call
            said: JsonObject = result.structured_content or {}
            if said.get("interrupted") and "utterance" in said:
                self._cuts.append((time.monotonic(), said["utterance"]))
            return result
        finally:
            self._saying -= 1
            self._returns.set()
            self._returns = asyncio.Event()

    async def returned(self, said: JsonObject) -> bool:
        """Whether a `say` returned `said`, the end of a push-to-talk Utterance that cut it.
        The `say` returns once the Utterance has ended: soon after, or just before."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(_CUT_RETURN_S):
                while self._saying and not self._cut(said):
                    await self._returns.wait()
        if not self._cut(said):
            return False
        self._cuts = [(at, cut) for at, cut in self._cuts if cut != said]
        return True

    def _cut(self, said: JsonObject) -> bool:
        """Whether a `say` returned `said` just now, cut by it."""
        stale = time.monotonic() - _CUT_RETURN_S
        self._cuts = [(at, cut) for at, cut in self._cuts if at >= stale]
        return any(cut == said for _, cut in self._cuts)

    def event(self, notification: Notification) -> _Event | None:
        """The channel event that `notification` makes, if any."""
        method, params = notification.method, notification.params
        if method in _GESTURES:
            return _Event(method, {"event": method, "address": params["address"]})
        if method == "mcp.result":
            outcome = {key: params[key] for key in ("result", "error") if key in params}
            meta = {"event": "result", "pending": str(params["pending"]), "tool": params["tool"]}
            return _Event(json.dumps(outcome), meta)
        if method == "utterance.start" and params["trigger"] == "button":
            self._pressed[params["utterance_id"]] = params["address"]
        elif method == "utterance.end":
            address = self._pressed.pop(params["utterance_id"], None)
            if address is not None and params["reason"] != "click":  # a click's gesture says it
                said: JsonObject = {"text": params.get("transcript", "")}
                if "lang" in params:
                    said["lang"] = params["lang"]
                said["reason"] = params["reason"]
                meta = {"event": "utterance", "address": address}
                meta |= {key: said[key] for key in ("lang", "reason") if key in said}
                return _Event(said["text"], meta, said)
        return None


@dataclass(frozen=True, slots=True)
class _Event:
    """A channel event: its content, and its meta."""

    content: str
    meta: dict[str, str]
    said: JsonObject | None = None
    """Of a push-to-talk Utterance, what the human said, as a `say` it cut returns it."""


def _urls(hub: str) -> tuple[str, str]:
    """The agent API's and the MCP server's URLs, on the hub at `hub`: `http://<host>:<port>`."""
    url = urlsplit(hub)
    ws = "wss" if url.scheme == "https" else "ws"
    return f"{ws}://{url.netloc}/agent", f"{url.scheme}://{url.netloc}/mcp"


def _refusal(e: ApiError) -> str:
    """Why the hub refused the shim a Session."""
    if e.code == "busy":
        since = time.strftime("%H:%M:%S", time.localtime(e.data["since"] / 1000))
        holder = e.data["holder"]
        return f"another agent holds the hub ({holder}, since {since}); --takeover takes it over"
    if e.code == "unauthorized":
        return "the hub refused the agent token"
    return f"the hub refused a Session: {e}"


def _cause(e: BaseException) -> BaseException:
    """The first exception at the root of `e`'s groups."""
    while isinstance(e, BaseExceptionGroup):
        e = cast(BaseExceptionGroup[BaseException], e).exceptions[0]
    return e


@contextlib.asynccontextmanager
async def _stdio() -> AsyncGenerator[tuple[_Read, _Write], None]:
    """MCP over the process's stdin and stdout, read and written by the loop. Unlike the MCP
    SDK's `stdio_server`, which reads stdin on a thread, it stops at once, stdin open or not."""
    loop = asyncio.get_running_loop()
    stdin = asyncio.StreamReader(limit=_LINE_LIMIT)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    stdout = asyncio.StreamWriter(transport, protocol, None, loop)
    to_server, read = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write, from_server = anyio.create_memory_object_stream[SessionMessage](0)

    async def reader() -> None:
        async with to_server:
            while line := await stdin.readline():
                try:
                    message = types.jsonrpc_message_adapter.validate_json(line, by_name=False)
                except Exception as e:
                    await to_server.send(e)
                    continue
                await to_server.send(SessionMessage(message))

    async def writer() -> None:
        async with from_server:
            async for message in from_server:
                json = message.message.model_dump_json(by_alias=True, exclude_unset=True)
                stdout.write(json.encode() + b"\n")
                await stdout.drain()

    pumps = [asyncio.ensure_future(reader()), asyncio.ensure_future(writer())]
    try:
        yield read, write
    finally:
        for pump in pumps:
            pump.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        transport.close()
