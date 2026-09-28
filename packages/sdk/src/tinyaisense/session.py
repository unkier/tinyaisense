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

from tinyaisense_wire import JsonObject
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

PING_INTERVAL = 2.0
PING_TIMEOUT = 4.0
"""The SDK pings the hub too, so that it notices a dead hub within ~6 s."""


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
        """Make a request and return its result. Raises `ApiError` or `SessionEnded`."""
        if self.ended is not None:
            raise SessionEnded(self.ended)
        request_id = next(self._ids)
        reply = self._pending[request_id] = asyncio.get_running_loop().create_future()
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            await self._ws.send(json.dumps(message, separators=(",", ":"), ensure_ascii=False))
            return await reply
        except ConnectionClosed:
            raise SessionEnded(self.ended or "closed") from None
        finally:
            self._pending.pop(request_id, None)

    async def devices(self) -> list[JsonObject]:
        """Every paired device: `{name, online, manifest}`."""
        return cast(list[JsonObject], (await self.call("devices"))["devices"])

    async def command(self, address: str, name: str, args: JsonObject | None = None) -> JsonObject:
        """Send a Kind command to the capability at `address`, e.g. `kitchen/led`, and return
        its result. An `led` command claims the device's LED from its Status."""
        return await self.call("command", address=address, name=name, args=args or {})

    async def led_release(self, device: str) -> None:
        """Hand `device`'s LED back to its Status."""
        await self.call("led.release", device=device)

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
        except ConnectionClosed:
            pass
        finally:
            self.ended = self.ended or "closed"
            for reply in self._pending.values():
                if not reply.done():
                    reply.set_exception(SessionEnded(self.ended))
            self._notifications.put_nowait(None)

    def _receive(self, message: JsonObject) -> None:
        if "method" in message:
            notification = Notification(message["method"], message.get("params", {}))
            if notification.method == "session.ended":
                self.ended = notification.params["reason"]
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
