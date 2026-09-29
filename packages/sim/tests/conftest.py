import asyncio
from collections.abc import AsyncIterator

import pytest
from tinyaisense_sim.pytest_plugin import SimulatorFactory
from tinyaisense_wire import Welcome

from .raw_hub import CONN_ID, Online, RawHub, RawUdp

ECHO = "m5stack-atom-echo"


@pytest.fixture
async def raw_hub() -> AsyncIterator[RawHub]:
    hub = RawHub()
    await hub.start()
    yield hub
    await hub.close()


@pytest.fixture
async def online(raw_hub: RawHub, simulator: SimulatorFactory) -> AsyncIterator[Online]:
    """A simulated Echo, welcomed by the raw hub, with the hub's UDP socket."""
    udp = await RawUdp.open()
    sim = simulator(raw_hub.address)
    added = asyncio.create_task(sim.add(ECHO, "kitchen"))
    connection = await raw_hub.accept()
    connection.send(Welcome("kitchen", CONN_ID, udp.port))
    device = await added
    yield Online(connection, udp, device)
    udp.close()
