import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

import pytest
import uvloop
from tinyaisense_hub import Config, Hub, OwnerClient, SpeechConfig, Timing
from tinyaisense_sim import Simulator
from tinyaisense_sim.pytest_plugin import SimulatorFactory

from .fake_polza import KEY, FakePolza, serve

FAST_TIMING = Timing(
    hello_timeout_ms=300,
    ping_interval_ms=100,
    dead_after_ms=800,
    agent_ping_interval_ms=100,
    agent_dead_after_ms=800,
)
NO_SPEECH = SpeechConfig()


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """Hub tests run on uvloop, the hub's loop. test_web.py runs on asyncio's own too, since
    it guards what the hub relies on in the loop's transports."""
    if item.path.name == "test_web.py":
        return {"asyncio": asyncio.new_event_loop, "uvloop": uvloop.new_event_loop}
    return {"uvloop": uvloop.new_event_loop}


class StartHub(Protocol):
    def __call__(
        self,
        *,
        auto_approve: bool = False,
        timing: Timing = FAST_TIMING,
        device_port: int = 0,
        mdns: str | None = None,
        host: str | None = "127.0.0.1",
        speech: SpeechConfig = ...,
    ) -> Awaitable[Hub]: ...


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agents connect straight to the hub, whatever proxy this machine has."""
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(name, "*")


@pytest.fixture
def polza(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePolza]:
    """A fake polza.ai, and its key in env `POLZA_API_KEY`."""
    monkeypatch.setenv("POLZA_API_KEY", KEY)
    yield from serve(FakePolza())


@pytest.fixture
def runtime_dir() -> Iterator[Path]:
    # Short, so that the owner socket path fits a sockaddr_un.
    with TemporaryDirectory(prefix="tas-") as path:
        yield Path(path)


@pytest.fixture
async def start_hub(tmp_path: Path, runtime_dir: Path) -> AsyncIterator[StartHub]:
    """Start an in-process hub on ephemeral ports, on loopback unless given a host (None: every
    address). Every hub shares one state dir.

    It advertises over mDNS only if given a service type to advertise, and has no speech
    engines unless given some.
    """
    hubs: list[Hub] = []

    async def start(
        *,
        auto_approve: bool = False,
        timing: Timing = FAST_TIMING,
        device_port: int = 0,
        mdns: str | None = None,
        host: str | None = "127.0.0.1",
        speech: SpeechConfig = NO_SPEECH,
    ) -> Hub:
        config = Config(
            agent_token="agent-token",
            device_port=device_port,
            http_port=0,
            timing=timing,
            speech=speech,
        )
        hub = Hub(
            config,
            state_dir=tmp_path / "state",
            runtime_dir=runtime_dir,
            auto_approve=auto_approve,
            host=host,
            mdns=mdns,
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
