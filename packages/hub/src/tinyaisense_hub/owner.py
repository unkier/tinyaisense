"""Owner powers: JSON-RPC 2.0 over a 0600 Unix socket, one JSON object per line.

This socket is the only way to reach them, so a leaked agent token can't pair devices.
"""

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast

from tinyaisense_wire import JsonObject

from .devices import Registry
from .errors import HubError, HubErrorCode

SOCKET_NAME = "owner.sock"

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_HUB_ERROR = -32000
"""Every refusal. `data.code` says which."""

_log = logging.getLogger("tinyaisense_hub.owner")


class OwnerServer:
    def __init__(self, registry: Registry, runtime_dir: Path) -> None:
        self.path = runtime_dir / SOCKET_NAME
        self._registry = registry
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._methods: dict[str, Callable[[_Params], Awaitable[Any]]] = {
            "devices": self._devices,
            "approve": self._approve,
            "rename": self._rename,
            "forget": self._forget,
            "identify": self._identify,
            "diag": self._diag,
            "set_volume": self._set_volume,
        }

    async def start(self) -> None:
        """Raises `HubError` if another hub is serving the socket."""
        runtime_dir = self.path.parent
        runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # It may predate us, e.g. in a shared /tmp: only a dir of our own is safe to lock down.
        if runtime_dir.is_symlink() or runtime_dir.stat().st_uid != os.getuid():
            raise HubError("unsafe_runtime_dir", f"{runtime_dir} isn't a directory of this user")
        os.chmod(runtime_dir, 0o700)
        if self.path.exists():
            try:
                _, writer = await asyncio.open_unix_connection(self.path)
            except OSError:
                self.path.unlink()  # stale, left by a hub that died
            else:
                writer.close()
                raise HubError("already_running", f"another hub is serving {self.path}")
        self._server = await asyncio.start_unix_server(self._client, self.path)
        os.chmod(self.path, 0o600)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        # uvloop's Server has no close_clients, so the clients are closed here.
        for writer in self._clients:
            writer.close()
        await self._server.wait_closed()
        self._server = None
        self.path.unlink(missing_ok=True)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        try:
            with contextlib.suppress(ConnectionError):
                while line := await reader.readline():
                    response = await self._handle(line)
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
        finally:
            self._clients.discard(writer)
            writer.close()

    async def _handle(self, line: bytes) -> JsonObject:
        try:
            request: object = json.loads(line)
        except ValueError as e:
            return _error(None, _PARSE_ERROR, str(e), "bad_request")
        if not isinstance(request, dict) or cast(JsonObject, request).get("jsonrpc") != "2.0":
            return _error(None, _INVALID_REQUEST, "not a JSON-RPC 2.0 request", "bad_request")
        request = cast(JsonObject, request)
        request_id = request.get("id")
        name = request.get("method")
        method = self._methods.get(name) if isinstance(name, str) else None
        if method is None:
            message = f"unknown method {name!r}"
            return _error(request_id, _METHOD_NOT_FOUND, message, "unknown_method")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return _error(request_id, _INVALID_PARAMS, "params must be an object", "invalid_params")
        try:
            result = await method(_Params(cast(JsonObject, params)))
        except _BadParams as e:
            return _error(request_id, _INVALID_PARAMS, str(e), "invalid_params")
        except HubError as e:
            return _error(request_id, _HUB_ERROR, e.message, e.code)
        except Exception:
            _log.exception("owner request %r failed", name)
            return _error(request_id, _INTERNAL_ERROR, "failed; see the hub log", "failed")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    async def _devices(self, params: _Params) -> JsonObject:
        params.done()
        return {"devices": self._registry.devices()}

    async def _approve(self, params: _Params) -> JsonObject:
        device, name = params.string("device"), params.optional_string("name")
        params.done()
        return await self._registry.approve(device, name)

    async def _rename(self, params: _Params) -> JsonObject:
        device, name = params.string("device"), params.string("name")
        params.done()
        return await self._registry.rename(device, name)

    async def _forget(self, params: _Params) -> JsonObject:
        device = params.string("device")
        params.done()
        await self._registry.forget(device)
        return {}

    async def _identify(self, params: _Params) -> JsonObject:
        device, seconds = params.string("device"), params.optional_integer("seconds")
        params.done()
        return await self._registry.identify(device, seconds)

    async def _diag(self, params: _Params) -> JsonObject:
        device = params.string("device")
        params.done()
        return await self._registry.diag(device)

    async def _set_volume(self, params: _Params) -> JsonObject:
        device, level = params.string("device"), params.integer("level")
        params.done()
        return await self._registry.set_volume(device, level)


class OwnerClient:
    """The owner's side of the socket. Refusals raise `HubError`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 1

    async def __aenter__(self) -> Self:
        self._reader, self._writer = await asyncio.open_unix_connection(self.path)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(ConnectionError):
                await self._writer.wait_closed()

    async def call(self, method: str, **params: Any) -> Any:
        assert self._reader is not None, "use `async with`"
        assert self._writer is not None
        request = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        self._next_id += 1
        self._writer.write(json.dumps(request).encode() + b"\n")
        line = await self._reader.readline()
        if not line:
            raise ConnectionError("the hub closed the owner socket")
        response = cast(JsonObject, json.loads(line))
        if "error" in response:
            error = cast(JsonObject, response["error"])
            code = cast(JsonObject, error.get("data", {})).get("code", "failed")
            raise HubError(cast(HubErrorCode, code), error["message"])
        return response["result"]

    async def devices(self) -> list[JsonObject]:
        return (await self.call("devices"))["devices"]

    async def approve(self, device: str, name: str | None = None) -> JsonObject:
        params = {"device": device} if name is None else {"device": device, "name": name}
        return await self.call("approve", **params)

    async def rename(self, device: str, name: str) -> JsonObject:
        return await self.call("rename", device=device, name=name)

    async def forget(self, device: str) -> None:
        await self.call("forget", device=device)

    async def identify(self, device: str, seconds: int | None = None) -> JsonObject:
        """Blink the device white for `seconds`, 1 to 300; the device's default is 5."""
        params = {"device": device} if seconds is None else {"device": device, "seconds": seconds}
        return await self.call("identify", **params)

    async def diag(self, device: str) -> JsonObject:
        """The device's health: docs/protocol/kinds/device.md#diag-hub-only."""
        return await self.call("diag", device=device)

    async def set_volume(self, device: str, level: int) -> JsonObject:
        """Set the device's speaker volume, 0 to 100, now if it's online and after every
        `welcome`. `applied` in the result says whether the device has it yet."""
        return await self.call("set_volume", device=device, level=level)


def _error(request_id: object, code: int, message: str, hub_code: str) -> JsonObject:
    error = {"code": code, "message": message, "data": {"code": hub_code}}
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


class _BadParams(Exception):
    pass


class _Params:
    def __init__(self, params: JsonObject) -> None:
        self._params = params
        self._read: set[str] = set()

    def string(self, key: str) -> str:
        value = self.optional_string(key)
        if value is None:
            raise _BadParams(f"{key} is missing")
        return value

    def optional_string(self, key: str) -> str | None:
        self._read.add(key)
        value = self._params.get(key)
        if value is not None and not isinstance(value, str):
            raise _BadParams(f"{key} must be a string")
        return value

    def integer(self, key: str) -> int:
        value = self.optional_integer(key)
        if value is None:
            raise _BadParams(f"{key} is missing")
        return value

    def optional_integer(self, key: str) -> int | None:
        self._read.add(key)
        value = self._params.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise _BadParams(f"{key} must be an integer")
        return value

    def done(self) -> None:
        if unknown := set(self._params) - self._read:
            raise _BadParams(f"unknown params: {', '.join(sorted(unknown))}")
