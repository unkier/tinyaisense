"""What the HIL tier checks its numbers by: the interrupt samples against the hub's `stats` and
the serial line, the soak's diag against its invariants, the POD's lag against its error budget,
and cut clicks against the positive control."""

import math

import pytest
from tinyaisense_hil.checks import (
    Diag,
    Edge,
    calibration_findings,
    click_findings,
    interrupt_findings,
    pod_lag,
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


def diag(
    minute: int,
    *,
    heap_min: int = 60_000,
    stack: int = 600,
    reconnects: int = 0,
    reason: str = "poweron",
    uptime_s: int | None = None,
) -> Diag:
    """The Echo's diag a `minute` into an hour's soak."""
    return Diag.from_json(
        minute * 60.0,
        {
            "uptime_s": minute * 60 + 30 if uptime_s is None else uptime_s,
            "reset_reason": reason,
            "heap_free": 100_000,
            "heap_min": heap_min,
            "rssi": -50,
            "reconnects": reconnects,
            "stack_hwm": {"ctrl": 1500, "udp_rx": stack, "console": 2000},
        },
    )


SOAK = [diag(minute) for minute in range(61)]


def test_a_soak_with_a_flat_heap_and_no_resets_passes() -> None:
    assert soak_findings(SOAK) == []


def test_heap_min_must_stay_at_40_kb_or_more() -> None:
    low = [*SOAK[:-1], diag(60, heap_min=40 * 1024 - 1)]

    findings = soak_findings(low)

    assert any("40 KB" in finding for finding in findings)


def test_heap_min_may_fall_while_the_soak_warms_up_then_must_stay_flat() -> None:
    warming = [diag(m, heap_min=60_000 - 400 * min(m, 15)) for m in range(61)]
    leaking = [diag(m, heap_min=60_000 - 100 * m) for m in range(61)]

    assert soak_findings(warming) == []
    (finding,) = soak_findings(leaking)
    assert "flat" in finding


def test_every_task_must_keep_over_512_bytes_of_stack() -> None:
    (finding,) = soak_findings([*SOAK[:30], diag(30, stack=512), *SOAK[31:]])

    assert "udp_rx" in finding


def test_a_reset_during_the_soak_fails() -> None:
    rebooted = [*SOAK[:30], *(diag(m, uptime_s=(m - 30) * 60, reconnects=4) for m in range(30, 61))]
    panicked = [*SOAK[:30], *(diag(m, reason="panic") for m in range(30, 61))]

    assert any("reset" in finding for finding in soak_findings(rebooted))
    assert any("panic" in finding for finding in soak_findings(panicked))


def test_the_echo_must_not_reconnect() -> None:
    (finding,) = soak_findings([*SOAK[:-1], diag(60, reconnects=1)])

    assert "1 reconnects" in finding


LAGS = [27.0 + 0.1 * (i % 7) for i in range(20)]
"""The POD's lag at 20 cuts: 27.0 to 27.6 ms."""


def test_the_pods_lag_is_the_median_of_the_cuts_it_heard() -> None:
    assert pod_lag([*LAGS, math.inf]) == pytest.approx(27.3)
    assert calibration_findings([*LAGS, math.inf]) == []


def test_the_pods_lag_must_hold_within_5_ms_of_its_median() -> None:
    (finding,) = calibration_findings([*LAGS[:-2], 33.0, 21.0])

    assert "5 ms" in finding


def test_the_pod_must_have_heard_most_cuts() -> None:
    (finding,) = calibration_findings([*LAGS[:9], *[math.inf] * 11])

    assert "9 of 20" in finding


CLEAN = [0.5, 1.2, -0.3, 2.1] * 5
CONTROL = [12.0, 7.5, 20.1, 2.0, 9.9]


def test_cuts_pass_unless_most_runs_have_a_cut_that_clicked() -> None:
    clicked = [*CLEAN[:-1], 8.5]

    assert click_findings([CLEAN, clicked, CLEAN], CONTROL) == []
    (finding,) = click_findings([clicked, CLEAN, clicked], CONTROL)
    assert "2 of 3 runs" in finding
    assert "8.5 dB" in finding


def test_a_cut_whose_end_wasnt_heard_isnt_shown_clean() -> None:
    (finding,) = click_findings([[*CLEAN, math.inf]] * 3, CONTROL)

    assert "3 of 3 runs" in finding


def test_the_click_check_holds_only_if_it_hears_most_of_the_positive_controls_clicks() -> None:
    (finding,) = click_findings([CLEAN] * 3, [12.0, 1.0, 2.0, 9.0, 0.5])

    assert "2 of 5" in finding
    assert click_findings([CLEAN] * 3, [12.0, 1.0, 20.0, 9.0, 0.5]) == []
    assert len(click_findings([CLEAN] * 3, [])) == 1
