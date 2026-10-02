"""The HIL acceptance report, in Markdown and JSON: a verdict over whatever ran, with the numbers,
the firmware and hub versions, and the rig."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from tinyaisense_hil.checks import Edge
from tinyaisense_hil.report import HilReport, Interrupt, Soak
from tinyaisense_hub.bench.gates import Gate

from .test_checks import EDGES, SOAK, diag

DATE = datetime(2026, 10, 2, 15, 30, tzinfo=UTC)
LATENCY: dict[str, object] = {"count": 600, "p50_ms": 4.0, "p99_ms": 4.2, "max_ms": 4.4}


def interrupt(*runs: list[Edge]) -> Interrupt:
    """The Interrupt gate's `runs`, which the hub's `stats` agree with if each has 200 edges
    the agent got."""
    gate = Gate("interrupt", "Interrupt", 99, 20, [[edge.ms for edge in run] for run in runs])
    return Interrupt(gate, [list(run) for run in runs], LATENCY)


def report(**parts: object) -> HilReport:
    base = HilReport(
        date=DATE,
        hub={"version": "0.1.0", "commit": "83277b9abcde"},
        firmware={"version": "83277b9", "elf": "2bc1ea9f43e7fce6", "flashed": True},
        rig={"port": "/dev/ttyUSB0", "echo_ip": "192.168.0.103", "iface": "enp5s0"},
        machine={
            "cpu": "Ryzen",
            "threads": 16,
            "governor": "schedutil",
            "loadavg": [0.5, 0.4, 0.3],
            "memory_gb": 32.0,
            "os": "Linux",
            "python": "3.14.0",
        },
    )
    return replace(base, **parts)  # pyright: ignore[reportArgumentType]


def test_a_report_of_everything_passing_is_green_and_says_what_ran() -> None:
    passing = report(
        interrupt=interrupt(EDGES, EDGES, EDGES),
        soak=Soak(minutes=60, cycles=60, diags=SOAK),
    )

    md = passing.markdown()

    assert passing.passed
    assert md.startswith("# HIL 83277b9, 2026-10-02 15:30 UTC: PASS")
    for shown in ("83277b9", "2bc1ea9f43e7fce6", "flashed", "0.1.0", "/dev/ttyUSB0", "schedutil"):
        assert shown in md
    assert "## Interrupt" in md
    assert "## Soak" in md


def test_a_report_fails_if_any_part_does_and_says_why() -> None:
    lost = [*EDGES[:-1], Edge("up", sent=0.0, t=None, got=None)]
    failing = report(soak=Soak(minutes=60, cycles=60, diags=[*SOAK[:-1], diag(60, reconnects=1)]))

    md = failing.markdown()

    assert not failing.passed
    assert ": FAIL" in md.splitlines()[0]
    assert "the Echo counted 1 reconnects" in md
    assert "## Interrupt" not in md  # it didn't run
    # The hub counted 600 edges, one the agent never got.
    assert not report(interrupt=interrupt(EDGES, lost, EDGES)).passed


def test_a_report_fails_if_a_part_broke_off_and_says_how() -> None:
    broke = report(soak=Soak(60, 60, SOAK), errors=["test_soak: SessionEnded: taken_over"])

    assert not broke.passed
    assert "test_soak: SessionEnded: taken_over" in broke.markdown()
    assert broke.to_json()["errors"] == ["test_soak: SessionEnded: taken_over"]


def test_a_soak_that_broke_off_before_its_first_diag_fails() -> None:
    broke = report(soak=Soak(minutes=60, cycles=0, diags=[], errors=["no hub"]))

    assert not broke.passed
    assert "no hub" in broke.markdown()


def test_a_report_is_written_as_markdown_and_json_named_by_its_date_and_commit(
    tmp_path: Path,
) -> None:
    written = report(interrupt=interrupt(EDGES, EDGES, EDGES)).write(tmp_path)

    assert [path.name for path in written] == [
        "2026-10-02-1530-hil-83277b9.md",
        "2026-10-02-1530-hil-83277b9.json",
    ]
    data = json.loads(written[1].read_text())
    assert data["passed"] is True
    assert data["firmware"]["elf"] == "2bc1ea9f43e7fce6"
    assert data["rig"]["iface"] == "enp5s0"
    assert data["interrupt"]["gate"]["runs"][0]["value_ms"] == 4.5
    assert data["interrupt"]["hub_latency"]["p99_ms"] == 4.2
    assert data["soak"] is None
