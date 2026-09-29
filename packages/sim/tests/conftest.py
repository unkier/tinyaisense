from collections.abc import AsyncIterator

import pytest

from .raw_hub import RawHub


@pytest.fixture
async def raw_hub() -> AsyncIterator[RawHub]:
    hub = RawHub()
    await hub.start()
    yield hub
    await hub.close()
