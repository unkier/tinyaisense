"""The pytest plugin: the `simulator` factory fixture.

It needs pytest-asyncio (`tinyaisense-sim[pytest]`). Without it, the plugin stays out of the
way and the fixture doesn't exist.
"""

from collections.abc import AsyncIterator
from pathlib import Path

from .simulator import Simulator


class SimulatorFactory:
    """Builds Simulators that close at the end of the test."""

    def __init__(self) -> None:
        self._sims: list[Simulator] = []

    def __call__(
        self, hub: str | tuple[str, int], state_dir: Path | None = None, *, seed: int = 0
    ) -> Simulator:
        sim = Simulator(hub, state_dir, seed=seed)
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
        """`simulator(hub, state_dir=None, *, seed=0)` makes a Simulator against `hub`."""
        factory = SimulatorFactory()
        yield factory
        await factory.close()
