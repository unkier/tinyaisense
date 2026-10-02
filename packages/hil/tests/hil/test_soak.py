"""The soak on the real Echo, an hour by default, from a power-on reset: a round of speaker, mic
and presses a minute. Its `diag`, each minute, must show heap_min flat and 40 KB or more, no
reset besides the power-on, every task over 512 B of stack to spare, and no reconnects."""

import asyncio
import random
import time

import pytest
import tinyaisense
from tinyaisense_hil.checks import Diag
from tinyaisense_hil.hub import HilHub
from tinyaisense_hil.report import HilReport, Soak
from tinyaisense_hub.bench.corpus import BenchError

from .conftest import Hil

# As long as it runs: the workflow bounds it.
pytestmark = [pytest.mark.hil, pytest.mark.timeout(0)]

ROUND_S = 60
RESET_S = 120
"""How long the Echo may have been up at the soak's first diag, after its reset."""
SPEAKER_MS = 3000
MIC_MS = 3000
PRESSES = 10


async def test_the_echo_holds_up_through_the_soak(hil: Hil, report: HilReport) -> None:
    soak = report.soak = Soak(minutes=hil.soak_minutes, cycles=0, diags=[])
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
                broke = await _round(hil, hub, rng)
                soak.errors += [f"round {cycle + 1}: {what}" for what in broke]
            except (BenchError, TimeoutError, tinyaisense.ApiError) as e:
                soak.errors.append(f"round {cycle + 1}: {type(e).__name__}: {e}")
            await asyncio.sleep(max(0.0, start + (cycle + 1) * ROUND_S - time.monotonic()))
            soak.diags.append(Diag.from_json(time.monotonic() - start, await hub.diag()))
            soak.cycles += 1

    assert soak.passed, soak.findings


async def _round(hil: Hil, hub: HilHub, rng: random.Random) -> list[str]:
    """The workload: what broke in it."""
    hub.forget()
    speaker = await hub.play(SPEAKER_MS)
    mic = await hub.record(MIC_MS)
    edges = await hub.presses(hil.console, PRESSES, rng)
    broke: list[str] = []
    if speaker["reason"] != "drained":
        broke.append(f"the speaker stream ended {speaker['reason']}")
    if mic["reason"] != "stopped":
        broke.append(f"the mic stream ended {mic['reason']}")
    if lost := sum(edge.got is None for edge in edges):
        broke.append(f"{lost} of {len(edges)} edges never reached the agent")
    if hub.offline:
        broke.append(f"the Echo went offline {hub.offline} times")
    return broke
