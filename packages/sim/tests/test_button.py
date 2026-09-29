"""The button Kind on a simulated device, driven by a hand-rolled hub:
docs/protocol/kinds/button.md."""

import asyncio

import pytest
from tinyaisense_wire import Event

from .raw_hub import CONN_ID, Online


async def test_a_press_goes_out_on_tcp_and_twice_over_udp_about_5_ms_apart(
    online: Online,
) -> None:
    loop = asyncio.get_running_loop()
    before = await online.clock()

    online.device.button.press()
    event = await online.connection.event()
    first = await online.udp.mirror()
    first_at = loop.time()
    second = await online.udp.mirror()
    second_at = loop.time()

    assert event == Event(0, "button", "down", event.ts, {})
    assert event.ts >= before
    assert first.conn_id == second.conn_id == CONN_ID
    assert first.event == second.event == event
    assert 0.004 <= second_at - first_at < 0.05


async def test_a_release_goes_out_as_up_after_down(online: Online) -> None:
    button = online.device.button

    button.press()
    button.press()  # it's down already
    button.release()
    down = await online.connection.event()
    up = await online.connection.event()

    assert up == Event(1, "button", "up", up.ts, {})
    assert up.ts > down.ts


async def test_delay_events_holds_the_tcp_copy_but_not_the_mirrors(online: Online) -> None:
    online.device.faults.delay_events_ms = 200

    online.device.button.press()
    mirror = await online.udp.mirror()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(online.connection.event(), 0.1)
    event = await online.connection.event()

    assert event == mirror.event


async def test_the_helpers_hold_the_button_down_as_long_as_they_say(online: Online) -> None:
    button = online.device.button

    await button.click(ms=50)
    await button.double_click(ms=50, gap_ms=100)
    await button.long_press(ms=300)
    events = [await online.connection.event() for _ in range(8)]

    edges = [(e.name, e.ts) for e in events]
    assert [name for name, _ in edges] == ["down", "up"] * 4
    ts = [t / 1000 for _, t in edges]
    held = [up - down for down, up in zip(ts[::2], ts[1::2], strict=True)]
    for ms, expected in zip(held, [50, 50, 50, 300], strict=True):
        assert expected - 2 <= ms < expected + 40
    assert 98 <= ts[4] - ts[3] < 140
