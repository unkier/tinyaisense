"""The Kind contracts: what docs/protocol/kinds/ says, as data the hub and Simulator consume."""

import re
from pathlib import Path

import pytest
from tinyaisense_wire import DEVICE_COMMANDS, KINDS, SchemaError, validate

KINDS_DOCS = Path(__file__).resolve().parents[3] / "docs" / "protocol" / "kinds"

ECHO_FORMATS = {
    "formats": [{"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": [10, 20]}]
}


def documented(doc: str, section: str) -> dict[str, bool]:
    """The `### \\`name\\`` headings of one `## section`, each mapped to its hub-only mark."""
    text = (KINDS_DOCS / f"{doc}.md").read_text(encoding="utf-8")
    match = re.search(rf"^## {section}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    body = match.group(1) if match else ""
    return {
        name: bool(hub_only)
        for name, hub_only in re.findall(r"^### `(\w+)`( \(hub-only\))?$", body, re.M)
    }


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_the_kind_doc_lists_the_contract_commands_and_events(kind: str) -> None:
    contract = KINDS[kind]
    assert documented(kind, "Commands") == {
        name: spec.hub_only for name, spec in contract.commands.items()
    }
    assert set(documented(kind, "Events")) == set(contract.events)


def test_the_device_doc_lists_the_device_level_commands() -> None:
    assert documented("device", "Commands") == {
        name: spec.hub_only for name, spec in DEVICE_COMMANDS.items()
    }


@pytest.mark.parametrize("doc", [*sorted(KINDS), "device"])
def test_the_kind_doc_links_its_contract_data(doc: str) -> None:
    link = f"packages/wire/src/tinyaisense_wire/contracts/{doc}.json"
    assert link in (KINDS_DOCS / f"{doc}.md").read_text(encoding="utf-8")


def test_agents_may_only_send_the_led_commands() -> None:
    # Streams go through `stream.*`, `say` and `listen`; the volume is an owner power.
    agent_commands = {
        (contract.kind, name)
        for contract in KINDS.values()
        for name, spec in contract.commands.items()
        if not spec.hub_only
    }
    assert agent_commands == {("led", "set"), ("led", "off")}
    assert all(spec.hub_only for spec in DEVICE_COMMANDS.values())


def test_every_command_times_out_after_the_default_second() -> None:
    commands = [c for k in KINDS.values() for c in k.commands.values()]
    assert {c.timeout_ms for c in [*commands, *DEVICE_COMMANDS.values()]} == {1000}


@pytest.mark.parametrize(
    "args",
    [
        {"color": "#00ff00"},
        {"color": "#FF8000", "brightness": 0, "pattern": "blink", "period_ms": 200, "index": 0},
        {"color": "#000000", "brightness": 100, "pattern": "pulse", "period_ms": 5000},
        {"color": "#00ff00", "glow": "extra args are ignored"},
    ],
)
def test_led_set_accepts(args: dict[str, object]) -> None:
    validate(KINDS["led"].commands["set"].args, args)


@pytest.mark.parametrize(
    ("args", "where"),
    [
        ({}, "args.color"),
        ({"color": "00ff00"}, "args.color"),
        ({"color": "#00ff00\n"}, "args.color"),
        ({"color": "#00ff0"}, "args.color"),
        ({"color": 65280}, "args.color"),
        ({"color": "#00ff00", "brightness": 101}, "args.brightness"),
        ({"color": "#00ff00", "brightness": -1}, "args.brightness"),
        ({"color": "#00ff00", "brightness": 50.0}, "args.brightness"),
        ({"color": "#00ff00", "brightness": True}, "args.brightness"),
        ({"color": "#00ff00", "pattern": "strobe"}, "args.pattern"),
        ({"color": "#00ff00", "period_ms": 199}, "args.period_ms"),
        ({"color": "#00ff00", "index": -1}, "args.index"),
        ([], "args"),
    ],
)
def test_led_set_rejects(args: object, where: str) -> None:
    with pytest.raises(SchemaError) as failure:
        validate(KINDS["led"].commands["set"].args, args, "args")
    assert failure.value.where == where


def test_set_reflex_arms_with_targets_or_disarms() -> None:
    schema = KINDS["button"].commands["set_reflex"].args
    fmt = {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": 10}
    validate(schema, {"armed": True, "stop": ["speaker"], "start": {"cap": "mic", "format": fmt}})
    validate(schema, {"armed": False})
    with pytest.raises(SchemaError):
        validate(schema, {"armed": True, "start": {"cap": "mic"}})
    with pytest.raises(SchemaError):
        validate(schema, {"armed": "yes"})
    with pytest.raises(SchemaError):
        validate(schema, {"armed": True, "stop": ["speaker", 1]})


def test_stream_commands_use_hub_range_ids_and_nullable_last_seq() -> None:
    speaker = KINDS["speaker"].commands
    fmt = {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": 10}
    validate(speaker["start"].args, {"stream": 127, "format": fmt, "prefill_ms": 40})
    with pytest.raises(SchemaError):
        validate(speaker["start"].args, {"stream": 128, "format": fmt})
    with pytest.raises(SchemaError):
        validate(speaker["start"].args, {"stream": 0, "format": fmt})
    validate(speaker["drain"].args, {"last_seq": None})
    validate(speaker["drain"].args, {"last_seq": 4294967295})
    with pytest.raises(SchemaError):
        validate(speaker["drain"].args, {"last_seq": 4294967296})
    validate(KINDS["mic"].events["started"].data, {"stream": 128, "format": fmt})
    with pytest.raises(SchemaError):
        validate(KINDS["mic"].events["started"].data, {"stream": 127, "format": fmt})


def test_the_echo_capability_params_match_their_kinds() -> None:
    validate(KINDS["mic"].params, ECHO_FORMATS)
    validate(KINDS["speaker"].params, ECHO_FORMATS)
    validate(KINDS["button"].params, {})
    validate(KINDS["led"].params, {"count": 1})
    with pytest.raises(SchemaError):
        validate(KINDS["mic"].params, {"formats": []})


def test_diag_reports_stack_high_water_marks_per_task() -> None:
    result = {
        "uptime_s": 86400,
        "reset_reason": "poweron",
        "heap_free": 112640,
        "heap_min": 98304,
        "rssi": -61,
        "reconnects": 2,
        "stack_hwm": {"ctrl": 1480, "spk": 912},
    }
    validate(DEVICE_COMMANDS["diag"].result, result)
    with pytest.raises(SchemaError) as failure:
        validate(DEVICE_COMMANDS["diag"].result, result | {"stack_hwm": {"ctrl": -1}}, "result")
    assert failure.value.where == "result.stack_hwm.ctrl"
