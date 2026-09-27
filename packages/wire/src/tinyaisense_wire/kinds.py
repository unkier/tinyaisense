"""The Kind contracts as data: events, commands, schemas, `hub_only` flags and timeouts.

The data lives in `contracts/*.json`, one file per Kind plus `device.json` for the
device-level commands. docs/protocol/kinds/ describes what it means.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Literal, TypeAlias

from .schema import check_schema

Schema: TypeAlias = Mapping[str, Any]
"""A JSON Schema, in the subset `tinyaisense_wire.validate` understands."""


@dataclass(frozen=True, slots=True)
class CommandContract:
    name: str
    args: Schema
    result: Schema
    hub_only: bool
    """Hub-only commands are left out of `devices` and rejected from agents."""
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class EventContract:
    name: str
    data: Schema


@dataclass(frozen=True, slots=True)
class KindContract:
    kind: str
    stream: Literal["up", "down"] | None
    params: Schema
    events: Mapping[str, EventContract]
    commands: Mapping[str, CommandContract]


def _load(name: str) -> dict[str, Any]:
    text = files("tinyaisense_wire").joinpath("contracts", f"{name}.json").read_text("utf-8")
    return json.loads(text)


def _schema(schema: Schema, where: str) -> Schema:
    check_schema(schema, where)
    return schema


def _commands(data: dict[str, Any], where: str) -> Mapping[str, CommandContract]:
    return MappingProxyType(
        {
            name: CommandContract(
                name,
                _schema(spec["args"], f"{where}.{name}.args"),
                _schema(spec["result"], f"{where}.{name}.result"),
                spec["hub_only"],
                spec["timeout_ms"],
            )
            for name, spec in data.items()
        }
    )


def _kind(name: str) -> KindContract:
    data = _load(name)
    events = {
        event: EventContract(event, _schema(spec["data"], f"{name}.{event}.data"))
        for event, spec in data["events"].items()
    }
    return KindContract(
        data["kind"],
        data["stream"],
        _schema(data["params"], f"{name}.params"),
        MappingProxyType(events),
        _commands(data["commands"], name),
    )


KINDS: Mapping[str, KindContract] = MappingProxyType(
    {contract.kind: contract for contract in map(_kind, ("mic", "speaker", "button", "led"))}
)
"""Every Kind this protocol version knows, by name."""

DEVICE_COMMANDS: Mapping[str, CommandContract] = _commands(_load("device")["commands"], "device")
"""The device-level commands: `cmd` without a `cap`."""
