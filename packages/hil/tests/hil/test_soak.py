"""The soak on the real Echo, an hour by default, from a power-on reset: a round of speaker, mic
and presses a minute, under each lossy scenario in turn, and every 10 minutes a blackout past
the hub's 6 s dead time, which forces a reconnect. Its `diag`, each minute, must show heap_min
flat and 40 KB or more, no reset besides the power-on, every task over 512 B of stack to spare,
and as many reconnects as were forced."""

import asyncio
import random
import time

import pytest
import tinyaisense
from tinyaisense_hil.checks import Diag
from tinyaisense_hil.hub import ONLINE_S, HilHub
from tinyaisense_hil.report import HilReport, Soak
from tinyaisense_hub.bench.corpus import BenchError

from .conftest import Hil

# As long as it runs: the workflow bounds it.
pytestmark = [pytest.mark.hil, pytest.mark.timeout(0)]

ROUND_S = 60
RESET_S = 120
"""How long the Echo may have been up at the soak's first diag, after its reset."""
ROTATION = ("loss2", "loss5-bursts", "jitter30")
FORCE_EVERY = 10
FORCE_AT = 5
"""The round of every 10 that forces a reconnect, instead of its workload."""
FORCED_BLACKOUT_S = 10
SPEAKER_MS = 3000
MIC_MS = 3000
PRESSES = 10


async def test_the_echo_holds_up_through_the_soak(hil: Hil, report: HilReport) -> None:
    soak = report.soak = Soak(minutes=hil.soak_minutes, cycles=0, forced=0, diags=[])
    rng = random.Random()
    hil.console.hard_reset()
    async with hil.hub() as hub:
        await hub.session.reflex_set(hub.device, armed=False)
        start = time.monotonic()
        soak.diags.append(Diag.from_json(0, await hub.diag()))
        if soak.diags[0].uptime_s > RESET_S:
            soak.errors.append(
                f"the Echo didn't reset at the start: it was up {soak.diags[0].uptime_s} s"
            )
        for cycle in range(max(1, round(soak.minutes * 60 / ROUND_S))):
            try:
                if cycle % FORCE_EVERY == FORCE_AT:
                    await _force_reconnect(hil, hub)
                    soak.forced += 1
                else:
                    broke = await _round(hil, hub, ROTATION[cycle % len(ROTATION)], rng)
                    soak.errors += [f"round {cycle + 1}, {what}" for what in broke]
            except (BenchError, TimeoutError, tinyaisense.ApiError) as e:
                soak.errors.append(f"round {cycle + 1}: {type(e).__name__}: {e}")
                await hil.netem.clear()
            await asyncio.sleep(max(0.0, start + (cycle + 1) * ROUND_S - time.monotonic()))
            soak.diags.append(Diag.from_json(time.monotonic() - start, await hub.diag()))
            soak.cycles += 1

    assert soak.passed, soak.findings


async def _round(hil: Hil, hub: HilHub, scenario: str, rng: random.Random) -> list[str]:
    """The workload, under `scenario`: what broke in it. What it loses is the fault tests'
    business."""
    hub.forget()
    await hil.netem.apply(scenario)
    try:
        speaker = await hub.play(SPEAKER_MS)
        mic = await hub.record(MIC_MS)
        edges = await hub.presses(hil.console, PRESSES, rng)
    finally:
        await hil.netem.clear()
    broke: list[str] = []
    if speaker["reason"] != "drained":
        broke.append(f"the speaker stream ended {speaker['reason']}")
    if mic["reason"] != "stopped":
        broke.append(f"the mic stream ended {mic['reason']}")
    if lost := sum(edge.got is None for edge in edges):
        broke.append(f"{lost} of {len(edges)} edges never reached the agent")
    if hub.offline:
        broke.append(f"the Echo went offline {hub.offline} times")
    return [f"{scenario}: {what}" for what in broke]


async def _force_reconnect(hil: Hil, hub: HilHub) -> None:
    """A blackout past the hub's dead time: the Echo goes offline and comes back."""
    hub.forget()
    await hil.netem.apply("blackout")
    try:
        await asyncio.sleep(FORCED_BLACKOUT_S)
    finally:
        await hil.netem.clear()
    await hub.back_online(ONLINE_S)
