"""The JSON control messages carried in type-1 TCP frames."""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, TypeAlias, cast

from .errors import WireError
from .kinds import KINDS
from .schema import SchemaError, validate

JsonObject: TypeAlias = dict[str, Any]

PROTOCOL = 1
"""The protocol major version this codec speaks."""
DEVICE_PORT = 8420
"""The hub's device port, TCP and UDP."""
HTTP_PORT = 8421
"""The hub's HTTP port: `/agent` and `/mcp`."""
MDNS_SERVICE = "_tinyaisense._tcp.local."

U16_MAX = 2**16 - 1
U32_MAX = 2**32 - 1
U64_MAX = 2**64 - 1
MAX_JSON_INT = 2**53 - 1
MAX_DEPTH = 16


_log = logging.getLogger("tinyaisense_wire")

_DEVICE_ID = re.compile("[0-9a-f]{12}")
_NAME = re.compile("[a-z0-9-]+")
"""Capability ids and device names."""
_MAX_TOKEN = 64


@dataclass(frozen=True, slots=True)
class Capability:
    id: str
    kind: str
    params: JsonObject = field(default_factory=dict[str, Any])

    def to_json(self) -> JsonObject:
        obj: JsonObject = {"id": self.id, "kind": self.kind}
        if self.params:
            obj["params"] = self.params
        return obj


@dataclass(frozen=True, slots=True)
class Manifest:
    """A device's self-description, fixed for the life of its Connection."""

    protocol: int
    device_id: str
    board: str
    firmware: str
    capabilities: tuple[Capability, ...]
    exclusive: tuple[tuple[str, ...], ...]

    @property
    def supported(self) -> tuple[Capability, ...]:
        """The capabilities of known Kinds. The others are ignored."""
        return tuple(cap for cap in self.capabilities if cap.kind in KINDS)

    def to_json(self) -> JsonObject:
        return {
            "protocol": self.protocol,
            "device_id": self.device_id,
            "board": self.board,
            "firmware": self.firmware,
            "capabilities": [cap.to_json() for cap in self.capabilities],
            "exclusive": [list(group) for group in self.exclusive],
        }

    @staticmethod
    def from_json(obj: JsonObject) -> Manifest:
        """Raises `WireError`: `protocol_mismatch` before any other field is looked at."""
        r = _Reader(obj, "manifest")
        protocol = r.uint("protocol", U32_MAX)
        if protocol != PROTOCOL:
            raise WireError("protocol_mismatch", f"protocol {protocol}, expected {PROTOCOL}")
        device_id = r.string("device_id")
        if not _DEVICE_ID.fullmatch(device_id):
            raise WireError("bad_message", "manifest.device_id must be 12 lowercase hex digits")
        capabilities = tuple(_capability(c) for c in r.objects("capabilities"))
        ids = [cap.id for cap in capabilities]
        if len(set(ids)) != len(ids):
            raise WireError("bad_message", "manifest.capabilities has duplicate ids")
        exclusive = tuple(tuple(r.strings(group)) for group in r.arrays("exclusive"))
        if unknown := {i for group in exclusive for i in group} - set(ids):
            raise WireError("bad_message", f"manifest.exclusive names unknown ids {unknown}")
        manifest = Manifest(
            protocol, device_id, r.string("board"), r.string("firmware"), capabilities, exclusive
        )
        for cap in capabilities:
            if cap.kind not in KINDS:
                _log.warning(
                    "device %s: ignoring capability %r of unknown kind %r",
                    device_id,
                    cap.id,
                    cap.kind,
                )
        return manifest


def _capability(obj: JsonObject) -> Capability:
    r = _Reader(obj, "capability")
    cap_id = r.string("id")
    if not _NAME.fullmatch(cap_id):
        raise WireError("bad_message", f"capability id {cap_id!r} must match [a-z0-9-]+")
    kind, params = r.string("kind"), r.object("params", default={})
    if kind in KINDS:
        try:
            validate(KINDS[kind].params, params, f"capability {cap_id!r} params")
        except SchemaError as e:
            raise WireError("bad_message", str(e)) from None
    return Capability(cap_id, kind, params)


@dataclass(frozen=True, slots=True)
class Hello:
    token: str | None
    udp_port: int
    manifest: Manifest

    def to_json(self) -> JsonObject:
        return {
            "type": "hello",
            "token": self.token,
            "udp_port": self.udp_port,
            "manifest": self.manifest.to_json(),
        }


@dataclass(frozen=True, slots=True)
class Welcome:
    name: str
    conn_id: int
    udp_port: int
    token: str | None = None
    """Present only on first Pairing."""

    def to_json(self) -> JsonObject:
        obj: JsonObject = {
            "type": "welcome",
            "name": self.name,
            "conn_id": self.conn_id,
            "udp_port": self.udp_port,
        }
        if self.token is not None:
            obj["token"] = self.token
        return obj


@dataclass(frozen=True, slots=True)
class Pending:
    def to_json(self) -> JsonObject:
        return {"type": "pending"}


@dataclass(frozen=True, slots=True)
class Error:
    """The hub refuses a `hello`, then closes the Connection."""

    code: str
    message: str
    protocol: int

    def to_json(self) -> JsonObject:
        return {
            "type": "error",
            "code": self.code,
            "message": self.message,
            "protocol": self.protocol,
        }


@dataclass(frozen=True, slots=True)
class Command:
    """`cmd`: a Command to capability `cap`, or to the device itself when `cap` is None."""

    id: int
    cap: str | None
    name: str
    args: JsonObject

    def to_json(self) -> JsonObject:
        obj: JsonObject = {"type": "cmd", "id": self.id}
        if self.cap is not None:
            obj["cap"] = self.cap
        return obj | {"name": self.name, "args": self.args}


@dataclass(frozen=True, slots=True)
class Reply:
    """A successful `reply`."""

    id: int
    result: JsonObject

    def to_json(self) -> JsonObject:
        return {"type": "reply", "id": self.id, "ok": True, "result": self.result}


@dataclass(frozen=True, slots=True)
class FailedReply:
    """A `reply` with `ok: false`."""

    id: int
    code: str
    message: str

    def to_json(self) -> JsonObject:
        error = {"code": self.code, "message": self.message}
        return {"type": "reply", "id": self.id, "ok": False, "error": error}


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    cap: str
    name: str
    ts: int
    data: JsonObject

    def to_json(self) -> JsonObject:
        return {
            "type": "event",
            "seq": self.seq,
            "cap": self.cap,
            "name": self.name,
            "ts": self.ts,
            "data": self.data,
        }


@dataclass(frozen=True, slots=True)
class Ping:
    t: int

    def to_json(self) -> JsonObject:
        return {"type": "ping", "t": self.t}


@dataclass(frozen=True, slots=True)
class Pong:
    t: int
    ts: int

    def to_json(self) -> JsonObject:
        return {"type": "pong", "t": self.t, "ts": self.ts}


Message: TypeAlias = (
    Hello | Welcome | Pending | Error | Command | Reply | FailedReply | Event | Ping | Pong
)


def _hello(r: _Reader) -> Hello:
    # The Manifest goes first, so that a protocol mismatch wins over any other problem.
    manifest = Manifest.from_json(r.object("manifest"))
    token = _token(r.nullable_string("token"))
    return Hello(token, r.uint("udp_port", U16_MAX, minimum=1), manifest)


def _welcome(r: _Reader) -> Welcome:
    name = r.string("name")
    if not _NAME.fullmatch(name):
        raise WireError("bad_message", f"welcome.name {name!r} must match [a-z0-9-]+")
    return Welcome(
        name,
        r.uint("conn_id", U32_MAX, minimum=1),
        r.uint("udp_port", U16_MAX, minimum=1),
        _token(r.optional_string("token")),
    )


def _token(token: str | None) -> str | None:
    if token is not None and (len(token) > _MAX_TOKEN or not token.isascii()):
        raise WireError("bad_message", f"a token is at most {_MAX_TOKEN} ASCII characters")
    return token


def _command(r: _Reader) -> Command:
    cmd_id = r.uint("id", U32_MAX)
    return Command(cmd_id, r.optional_string("cap"), r.string("name"), r.object("args"))


def _reply(r: _Reader) -> Reply | FailedReply:
    cmd_id = r.uint("id", U32_MAX)
    if r.boolean("ok"):
        return Reply(cmd_id, r.object("result"))
    error = _Reader(r.object("error"), "error")
    return FailedReply(cmd_id, error.string("code"), error.string("message"))


def _event(r: _Reader) -> Event:
    return Event(
        r.uint("seq", U32_MAX),
        r.string("cap"),
        r.string("name"),
        r.uint("ts", U64_MAX),
        r.object("data"),
    )


_DECODERS: dict[str, Callable[[_Reader], Message]] = {
    "hello": _hello,
    "welcome": _welcome,
    "pending": lambda r: Pending(),
    "error": lambda r: Error(r.string("code"), r.string("message"), r.uint("protocol", U32_MAX)),
    "cmd": _command,
    "reply": _reply,
    "event": _event,
    "ping": lambda r: Ping(r.uint("t", U64_MAX)),
    "pong": lambda r: Pong(r.uint("t", U64_MAX), r.uint("ts", U64_MAX)),
}


def decode_message(body: bytes) -> Message | None:
    """Decode one JSON message. Returns None for an unknown `type`. Raises `WireError`."""
    reader = _Reader(_parse(body), "message")
    decoder = _DECODERS.get(reader.string("type"))
    return None if decoder is None else decoder(reader)


def decode_event(body: bytes) -> Event:
    """Decode a JSON body that must hold an `event`, as an event mirror does."""
    message = decode_message(body)
    if not isinstance(message, Event):
        raise WireError("bad_message", "expected an event")
    return message


def encode_message(message: Message) -> bytes:
    """Encode one message as canonical JSON."""
    return _dump(message.to_json())


def _parse(body: bytes) -> JsonObject:
    """Parse a JSON body under the JSON rules of docs/protocol/device.md."""
    try:
        value: object = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_keys,
            parse_constant=_reject_constant,
        )
        if not isinstance(value, dict):
            raise ValueError("the top-level value isn't an object")
        obj = cast(JsonObject, value)
        _check_value(obj, 1)
    except (ValueError, RecursionError) as e:
        raise WireError("bad_json", str(e)) from None
    return obj


def _unique_keys(pairs: list[tuple[str, Any]]) -> JsonObject:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate key")
    return obj


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} isn't allowed")


def _check_value(value: object, depth: int) -> None:
    """Enforce what `json` lets through: depth, integer range, overflow, lone surrogates."""
    if isinstance(value, str):
        value.encode()  # UnicodeEncodeError, a ValueError, on a lone surrogate
    elif isinstance(value, bool):
        pass
    elif isinstance(value, int):
        if abs(value) > MAX_JSON_INT:
            raise ValueError(f"integer {value} is beyond +-(2^53 - 1)")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("number overflows a double")
    elif isinstance(value, dict | list):
        if depth > MAX_DEPTH:
            raise ValueError(f"nesting deeper than {MAX_DEPTH} levels")
        if isinstance(value, dict):
            items = cast(JsonObject, value)
            for key in items:
                key.encode()
            children: Iterable[object] = items.values()
        else:
            children = cast(list[object], value)
        for child in children:
            _check_value(child, depth + 1)


def _dump(obj: JsonObject) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


class _Reader:
    """Typed access to the fields of a JSON object. Raises `bad_message` on a bad field."""

    def __init__(self, obj: JsonObject, where: str) -> None:
        self._obj = obj
        self._where = where

    def _bad(self, key: str, problem: str) -> WireError:
        return WireError("bad_message", f"{self._where}.{key} {problem}")

    def _get(self, key: str) -> object:
        if key not in self._obj:
            raise self._bad(key, "is missing")
        return self._obj[key]

    def uint(self, key: str, maximum: int, *, minimum: int = 0) -> int:
        value = self._get(key)
        if type(value) is not int or not minimum <= value <= maximum:
            raise self._bad(key, f"must be an integer in {minimum}..{maximum}")
        return value

    def string(self, key: str) -> str:
        value = self._get(key)
        if not isinstance(value, str):
            raise self._bad(key, "must be a string")
        return value

    def boolean(self, key: str) -> bool:
        value = self._get(key)
        if not isinstance(value, bool):
            raise self._bad(key, "must be a boolean")
        return value

    def nullable_string(self, key: str) -> str | None:
        value = self._get(key)
        if value is not None and not isinstance(value, str):
            raise self._bad(key, "must be a string or null")
        return value

    def optional_string(self, key: str) -> str | None:
        return self.string(key) if key in self._obj else None

    def object(self, key: str, *, default: JsonObject | None = None) -> JsonObject:
        if default is not None and key not in self._obj:
            return default
        value = self._get(key)
        if not isinstance(value, dict):
            raise self._bad(key, "must be an object")
        return cast(JsonObject, value)

    def _array(self, key: str) -> list[object]:
        value = self._get(key)
        if not isinstance(value, list):
            raise self._bad(key, "must be an array")
        return cast(list[object], value)

    def objects(self, key: str) -> list[JsonObject]:
        items = self._array(key)
        if not all(isinstance(item, dict) for item in items):
            raise self._bad(key, "must be an array of objects")
        return cast(list[JsonObject], items)

    def arrays(self, key: str) -> list[list[object]]:
        items = self._array(key)
        if not all(isinstance(item, list) for item in items):
            raise self._bad(key, "must be an array of arrays")
        return cast(list[list[object]], items)

    def strings(self, items: list[object]) -> list[str]:
        if not all(isinstance(item, str) for item in items):
            raise WireError("bad_message", f"{self._where}: expected an array of strings")
        return cast(list[str], items)
