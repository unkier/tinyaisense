"""The Echo under each netem fault scenario, on its traffic only, both ways: each keeps the
Connection and every press, and its streams play through or deliver their timeline, losing
about what the network does. A press in a blackout still cuts the speaker, on the Echo itself.
A scenario runs until 2 runs pass or 2 miss: it fails only on 2 misses."""

import asyncio
import random
from typing import cast

import pytest
from tinyaisense_hil.checks import FaultRun, fault_findings
from tinyaisense_hil.hub import HilHub
from tinyaisense_hil.netem import SCENARIOS
from tinyaisense_hil.report import MISSES, HilReport

from .conftest import TIMEOUT_S, Hil

pytestmark = [pytest.mark.hil, pytest.mark.timeout(TIMEOUT_S)]

SPEAKER_MS = 3000
MIC_MS = 3000
PRESSES = 20
BLACKOUT_S = 3.0
CUT_PLAY_MS = 10_000
"""The speaker stream a press in the blackout cuts: longer than the blackout."""
AFTER_MS = 1000
LATE_S = 10
"""How long after the blackout what it held up may take to arrive."""


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_the_echo_rides_out_the_fault(hil: Hil, report: HilReport, scenario: str) -> None:
    findings: list[list[str]] = []
    rng = random.Random()
    async with hil.hub() as hub:
        while (misses := sum(map(bool, findings))) < MISSES and len(findings) - misses < 2:
            if scenario == "blackout":
                run = await _blackout(hil, hub)
            else:
                run = await _lossy(hil, hub, scenario, rng)
            report.faults.append(run)
            findings.append(fault_findings(run))

    assert misses < MISSES, findings


async def _lossy(hil: Hil, hub: HilHub, scenario: str, rng: random.Random) -> FaultRun:
    """A speaker stream played through, a mic stream, then presses, with the Reflex
    disarmed: a press only reports."""
    await hub.session.reflex_set(hub.device, armed=False)
    hub.forget()
    before = await hub.diag()
    netem = await hil.netem.apply(scenario)
    try:
        speaker = await hub.play(SPEAKER_MS)
        mic = await hub.record(MIC_MS)
        edges = await hub.presses(hil.console, PRESSES, rng)
        after = await hub.diag()
    finally:
        await hil.netem.clear()
    return FaultRun(
        scenario,
        edges,
        list(hub.events),
        hub.offline,
        _reconnects(before, after),
        speaker,
        SPEAKER_MS,
        mic,
        MIC_MS,
        netem=netem,
    )


async def _blackout(hil: Hil, hub: HilHub) -> FaultRun:
    """A press while a speaker stream plays and the network is out, with the Reflex armed;
    then, after the blackout, a speaker stream played through."""
    await hub.session.reflex_set(hub.device, armed=True)
    hub.forget()
    before = await hub.diag()
    speaker = await hub.start_tone(CUT_PLAY_MS)
    await asyncio.sleep(0.5)
    netem = await hil.netem.apply("blackout")
    try:
        await asyncio.sleep(0.5)
        down = hil.console.press("down")
        await asyncio.sleep(0.1)
        up = hil.console.press("up")
        await asyncio.sleep(BLACKOUT_S - 0.6)
    finally:
        await hil.netem.clear()
    edges = [await hub.got("down", down, LATE_S), await hub.got("up", up, LATE_S)]
    cut = await hub.ended(speaker, LATE_S)
    played = await hub.play(AFTER_MS)
    after = await hub.diag()
    return FaultRun(
        "blackout",
        edges,
        list(hub.events),
        hub.offline,
        _reconnects(before, after),
        cut,
        CUT_PLAY_MS,
        None,
        0,
        after=played,
        after_ms=AFTER_MS,
        netem=netem,
    )


def _reconnects(before: dict[str, object], after: dict[str, object]) -> int:
    return cast(int, after["reconnects"]) - cast(int, before["reconnects"])
