"""The device side of a Connection, driven by a hand-rolled hub that can misbehave."""

import asyncio

from tinyaisense_sim.pytest_plugin import SimulatorFactory
from tinyaisense_sim.simulator import DEAD_AFTER_MS
from tinyaisense_wire import Pending, Ping, Pong

from .raw_hub import RawConnection, RawHub

ECHO = "m5stack-atom-echo"


async def pending_device(
    raw_hub: RawHub, simulator: SimulatorFactory, dead_after_ms: int = DEAD_AFTER_MS
) -> RawConnection:
    """Start a simulated Echo against `raw_hub` and leave it pending."""
    sim = simulator(raw_hub.address, dead_after_ms=dead_after_ms)
    added = asyncio.create_task(sim.add(ECHO, "kitchen"))
    connection = await raw_hub.accept()
    connection.send(Pending())
    await added
    return connection


async def test_a_device_answers_a_ping_with_its_own_clock(
    raw_hub: RawHub, simulator: SimulatorFactory
) -> None:
    connection = await pending_device(raw_hub, simulator)

    connection.send(Ping(123))
    first = await connection.receive()
    connection.send(Ping(456))
    second = await connection.receive()

    assert isinstance(first, Pong)
    assert isinstance(second, Pong)
    assert (first.t, second.t) == (123, 456)
    assert second.ts > first.ts


async def test_a_device_drops_a_silent_hub_and_reconnects(
    raw_hub: RawHub, simulator: SimulatorFactory
) -> None:
    connection = await pending_device(raw_hub, simulator, dead_after_ms=300)

    assert await connection.until_closed() == []
    await raw_hub.accept()


async def test_pings_from_the_hub_keep_the_connection_alive(
    raw_hub: RawHub, simulator: SimulatorFactory
) -> None:
    connection = await pending_device(raw_hub, simulator, dead_after_ms=400)

    # Twenty pings, 50 ms apart, outlast the dead timeout more than twice over.
    for t in range(20):
        connection.send(Ping(t))
        pong = await connection.receive()
        assert isinstance(pong, Pong)
        assert pong.t == t
        await asyncio.sleep(0.05)
