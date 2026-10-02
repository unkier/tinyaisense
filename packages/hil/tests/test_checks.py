"""What the HIL tier checks its numbers by: the interrupt samples against the hub's `stats` and
the serial line, each fault scenario against its expected behaviour, and the soak's diag
against its invariants."""

import math
from dataclasses import replace

from tinyaisense_hil.checks import (
    Diag,
    Edge,
    FaultRun,
    fault_findings,
    interrupt_findings,
    soak_findings,
)

EDGES = [
    Edge("down" if i % 2 == 0 else "up", sent=1000.0 * i, t=1000 * i + 2, got=1000.0 * i + 6.5)
    for i in range(200)
]
"""200 edges, each 2 ms from the serial write to the edge on the Echo, and 4.5 ms from there to
the agent."""


def stats(count: int = 200, p99_ms: float = 4.2) -> dict[str, object]:
    return {"count": count, "p50_ms": 4.0, "p99_ms": p99_ms, "max_ms": p99_ms}


def test_an_edge_is_timed_from_the_echo_to_the_agent_and_bounded_by_the_serial_write() -> None:
    edge = EDGES[1]

    assert edge.ms == 4.5
    assert edge.bound_ms == 6.5


def test_a_lost_edge_is_the_slowest() -> None:
    lost = Edge("down", sent=10.0, t=None, got=None)

    assert lost.ms == math.inf
    assert lost.bound_ms == math.inf


def test_samples_that_agree_with_the_hub_and_the_serial_line_pass() -> None:
    assert interrupt_findings(EDGES, stats()) == []


def test_the_hub_must_have_counted_every_edge_the_agent_got() -> None:
    lost = [*EDGES[:-1], Edge("up", sent=1e6, t=None, got=None)]

    assert interrupt_findings(lost, stats(199)) == []
    (finding,) = interrupt_findings(EDGES, stats(201))
    assert "201 edges" in finding


def test_the_hubs_p99_must_agree_with_the_agents() -> None:
    (finding,) = interrupt_findings(EDGES, stats(p99_ms=1.5))

    assert "p99" in finding


def test_an_edge_the_hub_puts_well_before_its_serial_write_means_the_clock_sync_is_off() -> None:
    early = Edge("down", sent=100.0, t=96, got=104.0)  # 4 ms before it was sent for

    (finding,) = interrupt_findings([*EDGES[1:], early], stats())
    assert "clock" in finding


def ended(reason: str, **stats: int) -> dict[str, object]:
    return {"reason": reason, "stats": stats}


SPEAKER = ended("drained", played_ms=3000, lost=6, underruns=0, underrun_ms=0)
MIC = ended("stopped", frames=300, lost=6, dropped=0, skipped=0)
LOSS2 = FaultRun(
    scenario="loss2",
    edges=EDGES[:20],
    events=["down", "up"] * 10,
    offline=0,
    reconnects=0,
    speaker=SPEAKER,
    speaker_ms=3000,
    mic=MIC,
    mic_ms=3000,
)


def test_a_scenario_that_kept_the_connection_and_every_press_passes() -> None:
    assert fault_findings(LOSS2) == []


def test_the_connection_must_survive_every_scenario() -> None:
    findings = fault_findings(replace(LOSS2, offline=1, reconnects=1))

    assert len(findings) == 2
    assert all("offline" in f or "reconnect" in f for f in findings)


def test_every_press_must_reach_the_agent_once_in_order() -> None:
    lost = [*EDGES[:19], Edge("up", sent=0.0, t=None, got=None)]

    assert fault_findings(replace(LOSS2, edges=lost, events=["down", "up"] * 9 + ["down"]))
    assert fault_findings(replace(LOSS2, events=["down", "up"] * 10 + ["up"]))
    assert fault_findings(replace(LOSS2, events=["down", "down"] + ["up", "up"] * 9))


def test_a_speaker_stream_must_play_through_and_lose_about_what_the_network_does() -> None:
    assert fault_findings(replace(LOSS2, speaker=ended("disconnected", played_ms=900, lost=0)))
    assert fault_findings(replace(LOSS2, speaker=ended("drained", played_ms=2500, lost=6)))
    (finding,) = fault_findings(replace(LOSS2, speaker=ended("drained", played_ms=3000, lost=30)))
    assert "lost" in finding
    lossy = replace(LOSS2, scenario="loss5-bursts")
    assert fault_findings(replace(lossy, speaker=ended("drained", played_ms=3000, lost=30))) == []


def test_a_mic_stream_must_deliver_its_whole_timeline() -> None:
    (finding,) = fault_findings(replace(LOSS2, mic=ended("stopped", frames=100, lost=0)))
    assert "frames" in finding
    assert fault_findings(replace(LOSS2, mic=ended("stopped", frames=300, lost=60)))


def test_jitter_must_lose_next_to_nothing() -> None:
    jitter = replace(LOSS2, scenario="jitter30", mic=ended("stopped", frames=300, lost=0))

    assert fault_findings(replace(jitter, speaker=ended("drained", played_ms=3000, lost=10)))
    assert fault_findings(replace(jitter, speaker=ended("drained", played_ms=3000, lost=0))) == []


BLACKOUT = replace(
    LOSS2,
    scenario="blackout",
    edges=EDGES[:2],
    events=["down", "up"],
    speaker=ended("reflex", played_ms=1500, lost=0),
    speaker_ms=6000,
    mic=None,
    mic_ms=0,
    after=ended("drained", played_ms=1000, lost=0),
    after_ms=1000,
)


def test_a_press_in_a_blackout_cuts_the_speaker_and_the_echo_recovers_after() -> None:
    assert fault_findings(BLACKOUT) == []


def test_a_press_in_a_blackout_that_didnt_cut_the_speaker_fails() -> None:
    (finding,) = fault_findings(replace(BLACKOUT, speaker=ended("drained", played_ms=6000)))

    assert "reflex" in finding


def test_an_echo_that_doesnt_play_after_a_blackout_fails() -> None:
    (finding,) = fault_findings(replace(BLACKOUT, after=ended("stopped", played_ms=0)))

    assert "after" in finding


def diag(
    minute: int,
    *,
    heap_min: int = 60_000,
    stack: int = 600,
    reconnects: int | None = None,
    reason: str = "poweron",
    uptime_s: int | None = None,
) -> Diag:
    """The Echo's diag a `minute` into an hour's soak, which forced a reconnect every 10."""
    return Diag.from_json(
        minute * 60.0,
        {
            "uptime_s": minute * 60 + 30 if uptime_s is None else uptime_s,
            "reset_reason": reason,
            "heap_free": 100_000,
            "heap_min": heap_min,
            "rssi": -50,
            "reconnects": (minute + 5) // 10 if reconnects is None else reconnects,
            "stack_hwm": {"ctrl": 1500, "udp_rx": stack, "console": 2000},
        },
    )


SOAK = [diag(minute) for minute in range(61)]
FORCED = 6


def test_a_soak_with_a_flat_heap_and_no_resets_passes() -> None:
    assert soak_findings(SOAK, FORCED) == []


def test_heap_min_must_stay_at_40_kb_or_more() -> None:
    low = [*SOAK[:-1], diag(60, heap_min=40 * 1024 - 1)]

    findings = soak_findings(low, FORCED)

    assert any("40 KB" in finding for finding in findings)


def test_heap_min_may_fall_while_the_soak_warms_up_then_must_stay_flat() -> None:
    warming = [diag(m, heap_min=60_000 - 400 * min(m, 15)) for m in range(61)]
    leaking = [diag(m, heap_min=60_000 - 100 * m) for m in range(61)]

    assert soak_findings(warming, FORCED) == []
    (finding,) = soak_findings(leaking, FORCED)
    assert "flat" in finding


def test_every_task_must_keep_over_512_bytes_of_stack() -> None:
    (finding,) = soak_findings([*SOAK[:30], diag(30, stack=512), *SOAK[31:]], FORCED)

    assert "udp_rx" in finding


def test_a_reset_during_the_soak_fails() -> None:
    rebooted = [*SOAK[:30], *(diag(m, uptime_s=(m - 30) * 60, reconnects=4) for m in range(30, 61))]
    panicked = [*SOAK[:30], *(diag(m, reason="panic") for m in range(30, 61))]

    assert any("reset" in finding for finding in soak_findings(rebooted, FORCED))
    assert any("panic" in finding for finding in soak_findings(panicked, FORCED))


def test_the_echo_must_reconnect_only_when_forced() -> None:
    (finding,) = soak_findings([*SOAK[:-1], diag(60, reconnects=7)], FORCED)

    assert "7 reconnects" in finding
