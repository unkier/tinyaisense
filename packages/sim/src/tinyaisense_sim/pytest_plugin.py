"""The pytest plugin: the `simulator` factory fixture.

It needs pytest-asyncio (`tinyaisense-sim[pytest]`). Without it, the plugin stays out of the
way and the fixture doesn't exist.
"""

from collections.abc import AsyncIterator
from pathlib import Path

from tinyaisense_wire import MDNS_SERVICE

from .simulator import DEAD_AFTER_MS, OUTPUT_LATENCY_MS, Simulator


class SimulatorFactory:
    """Builds Simulators that close at the end of the test."""

    def __init__(self) -> None:
        self._sims: list[Simulator] = []

    def __call__(
        self,
        hub: str | tuple[str, int] | None = None,
        state_dir: Path | None = None,
        *,
        seed: int = 0,
        dead_after_ms: int = DEAD_AFTER_MS,
        mdns_service: str = MDNS_SERVICE,
        output_latency_ms: int = OUTPUT_LATENCY_MS,
        trace: Path | None = None,
    ) -> Simulator:
        sim = Simulator(
            hub,
            state_dir,
            seed=seed,
            dead_after_ms=dead_after_ms,
            mdns_service=mdns_service,
            output_latency_ms=output_latency_ms,
            trace=trace,
        )
        self._sims.append(sim)
        return sim

    async def close(self) -> None:
        for sim in self._sims:
            await sim.close()


try:
    import pytest_asyncio
except ImportError:
    pass
else:

    @pytest_asyncio.fixture
    async def simulator() -> AsyncIterator[SimulatorFactory]:
        """`simulator(hub=None, state_dir=None, **options)` makes a Simulator: see `Simulator`."""
        factory = SimulatorFactory()
        yield factory
        await factory.close()
