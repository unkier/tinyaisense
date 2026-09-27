"""Finding the hub over mDNS, as the firmware does."""

import asyncio
from collections.abc import Awaitable, Callable

from tinyaisense_wire import PROTOCOL
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

type Resolve = Callable[[], Awaitable[tuple[str, int]]]
"""Where the hub is, as `(host, port)`."""

_QUERY_TIMEOUT_MS = 3000


class Mdns:
    """Resolves the hub over mDNS: the first hub found whose `proto` is our major."""

    def __init__(self, service: str) -> None:
        self._service = service
        self._zeroconf: AsyncZeroconf | None = None

    async def resolve(self) -> tuple[str, int]:
        """A fresh query each time. Waits until a hub turns up."""
        if self._zeroconf is None:
            self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
        zeroconf = self._zeroconf.zeroconf
        names: asyncio.Queue[str] = asyncio.Queue()

        def changed(name: str, state_change: ServiceStateChange, **_: object) -> None:
            if state_change is not ServiceStateChange.Removed:
                names.put_nowait(name)

        browser = AsyncServiceBrowser(zeroconf, self._service, handlers=[changed])
        try:
            while True:
                info = AsyncServiceInfo(self._service, await names.get())
                if not await info.async_request(zeroconf, _QUERY_TIMEOUT_MS):
                    continue
                addresses = info.parsed_addresses(IPVersion.V4Only)
                ours = info.properties.get(b"proto") == str(PROTOCOL).encode()
                if ours and addresses and info.port:
                    return addresses[0], info.port
        finally:
            await browser.async_cancel()

    async def close(self) -> None:
        if self._zeroconf is not None:
            await self._zeroconf.async_close()
            self._zeroconf = None
