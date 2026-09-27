from collections.abc import AsyncIterator, Awaitable, Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

import pytest
from tinyaisense_hub import Config, Hub, OwnerClient, Timing
from tinyaisense_sim import Simulator
from tinyaisense_sim.pytest_plugin import SimulatorFactory

FAST_TIMING = Timing(hello_timeout_ms=300)


class StartHub(Protocol):
    def __call__(self, *, auto_approve: bool = False) -> Awaitable[Hub]: ...


@pytest.fixture
def runtime_dir() -> Iterator[Path]:
    # Short, so that the owner socket path fits a sockaddr_un.
    with TemporaryDirectory(prefix="tas-") as path:
        yield Path(path)


@pytest.fixture
async def start_hub(tmp_path: Path, runtime_dir: Path) -> AsyncIterator[StartHub]:
    """Start an in-process hub on ephemeral ports. Every hub shares one state dir."""
    hubs: list[Hub] = []

    async def start(*, auto_approve: bool = False) -> Hub:
        config = Config(agent_token="agent-token", device_port=0, http_port=0, timing=FAST_TIMING)
        hub = Hub(
            config,
            state_dir=tmp_path / "state",
            runtime_dir=runtime_dir,
            auto_approve=auto_approve,
            host="127.0.0.1",
        )
        await hub.start()
        hubs.append(hub)
        return hub

    yield start
    for hub in hubs:
        await hub.close()


@pytest.fixture
async def hub(start_hub: StartHub) -> Hub:
    return await start_hub()


@pytest.fixture
async def owner(hub: Hub) -> AsyncIterator[OwnerClient]:
    async with OwnerClient(hub.owner_socket) as owner:
        yield owner


@pytest.fixture
def sim(hub: Hub, simulator: SimulatorFactory) -> Simulator:
    return simulator(hub.device_address)
