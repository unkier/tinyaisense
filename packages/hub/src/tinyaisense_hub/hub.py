"""The hub process: the device ports, the owner socket and the state behind them."""

import asyncio
from pathlib import Path
from types import TracebackType
from typing import Self, cast

from .config import Config
from .devices import Registry
from .owner import OwnerServer
from .state import StateStore


class Hub:
    """One hub. `start` it, or use `async with`."""

    def __init__(
        self,
        config: Config,
        *,
        state_dir: Path,
        runtime_dir: Path,
        auto_approve: bool = False,
        host: str | None = None,
    ) -> None:
        """`host` is the address to serve devices on; all interfaces if None."""
        self.config = config
        self._state_dir = state_dir
        self._runtime_dir = runtime_dir
        self._auto_approve = auto_approve
        self._host = host
        self._udp: asyncio.DatagramTransport | None = None
        self._tcp: asyncio.Server | None = None
        self._registry: Registry | None = None
        self._owner: OwnerServer | None = None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def device_address(self) -> tuple[str, int]:
        """The device TCP address, `(host, port)`."""
        assert self._tcp is not None, "the hub isn't running"
        host, port = cast(tuple[str, int], self._tcp.sockets[0].getsockname()[:2])
        return host, port

    @property
    def udp_port(self) -> int:
        assert self._udp is not None, "the hub isn't running"
        return cast(tuple[str, int], self._udp.get_extra_info("sockname"))[1]

    @property
    def owner_socket(self) -> Path:
        assert self._owner is not None, "the hub isn't running"
        return self._owner.path

    async def start(self) -> None:
        """Raises `HubError`, `StateError` or `OSError`, having released what it took."""
        try:
            await self._start()
        except BaseException:
            await self.close()
            raise

    async def _start(self) -> None:
        loop = asyncio.get_running_loop()
        port = self.config.device_port
        self._udp, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=(self._host or "0.0.0.0", port)
        )
        self._registry = Registry(
            StateStore(self._state_dir),
            udp_port=self.udp_port,
            timing=self.config.timing,
            auto_approve=self._auto_approve,
        )
        self._tcp = await loop.create_server(self._registry.connection, self._host, port)
        self._owner = OwnerServer(self._registry, self._runtime_dir)
        await self._owner.start()

    async def close(self) -> None:
        owner, tcp, udp, registry = self._owner, self._tcp, self._udp, self._registry
        self._owner = self._tcp = self._udp = self._registry = None
        if owner is not None:
            await owner.close()
        if tcp is not None:
            tcp.close()
            tcp.close_clients()
            await tcp.wait_closed()
        if udp is not None:
            udp.close()
        if registry is not None:
            await registry.close()
