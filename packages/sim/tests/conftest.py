import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from tinyaisense_sim import Board
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


type WelcomeDevice = Callable[[str | Board], Awaitable[Online]]


@pytest.fixture
async def welcome(raw_hub: RawHub, simulator: SimulatorFactory) -> AsyncIterator[WelcomeDevice]:
    """`await welcome(board)`: a simulated device of `board`, welcomed by the raw hub, with the
    hub's UDP socket."""
    udps: list[RawUdp] = []

    async def welcome(board: str | Board) -> Online:
        udp = await RawUdp.open()
        udps.append(udp)
        added = asyncio.create_task(simulator(raw_hub.address).add(board, "kitchen"))
        connection = await raw_hub.accept()
        connection.send(Welcome("kitchen", CONN_ID, udp.port))
        return Online(connection, udp, await added)

    yield welcome
    for udp in udps:
        udp.close()


@pytest.fixture
async def online(welcome: WelcomeDevice) -> Online:
    """A simulated Echo, welcomed by the raw hub, with the hub's UDP socket."""
    return await welcome(ECHO)
