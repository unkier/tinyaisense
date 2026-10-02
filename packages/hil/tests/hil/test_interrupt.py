"""The Interrupt gate on the real Echo: injected button edges, from the edge on the Echo to the
agent, p99 within 20 ms in 2 of 3 runs, cross-checked against the hub's own `stats`. The Reflex
is armed, as a Session leaves it: each run's first press cuts a speaker stream, and every press
opens the mic on the Echo."""

import contextlib
import random

import pytest
import tinyaisense
from tinyaisense_hil.checks import Edge
from tinyaisense_hil.report import HilReport, Interrupt
from tinyaisense_hub.bench.gates import Gate

from .conftest import TIMEOUT_S, Hil

pytestmark = [pytest.mark.hil, pytest.mark.timeout(TIMEOUT_S)]

RUNS = 3
PRESSES = 100
"""A run's: 200 edges."""
PLAY_MS = 60_000
"""The speaker stream a run's first press cuts: longer than a run."""


async def test_injected_edges_reach_the_agent_within_20_ms_p99(hil: Hil, report: HilReport) -> None:
    gate = Gate("interrupt", "Interrupt: injected `btn` edge → agent socket", 99, 20)
    runs: list[list[Edge]] = []
    rng = random.Random()
    async with hil.hub() as hub:
        for _ in range(RUNS):
            speaker = await hub.start_tone(PLAY_MS)
            edges = await hub.presses(hil.console, PRESSES, rng)
            with contextlib.suppress(tinyaisense.ApiError):  # the first press cut it
                await hub.session.stream_stop(speaker)
            runs.append(edges)
            gate.runs.append([edge.ms for edge in edges])
        latency = await hub.process.button_latency()

    report.interrupt = Interrupt(gate, runs, latency)

    assert report.interrupt.passed, (
        f"p99 by run: {[round(gate.value(i), 1) for i in range(RUNS)]} ms; "
        f"findings: {report.interrupt.findings}"
    )
