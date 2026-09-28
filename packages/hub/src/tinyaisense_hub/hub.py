"""The hub process: the device ports, the HTTP port, the owner socket and the state behind them."""

import asyncio
from pathlib import Path
from types import TracebackType
from typing import Self, cast

from tinyaisense_wire import MDNS_SERVICE

from .agent_api import agent_app
from .config import Config
from .core import Core
from .devices import Registry
from .discovery import Advertiser
from .owner import OwnerServer
from .session import Sessions
from .state import StateStore
from .web import HttpServer


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
        mdns: str | None = MDNS_SERVICE,
    ) -> None:
        """`host` is the address to serve devices and agents on; all interfaces if None.

        `mdns` is the service type to advertise the device port as; None not to advertise.
        """
        self.config = config
        self._state_dir = state_dir
        self._runtime_dir = runtime_dir
        self._auto_approve = auto_approve
        self._host = host
        self._mdns = mdns
        self._udp: asyncio.DatagramTransport | None = None
        self._tcp: asyncio.Server | None = None
        self._registry: Registry | None = None
        self._core: Core | None = None
        self._http: HttpServer | None = None
        self._owner: OwnerServer | None = None
        self._advertiser: Advertiser | None = None

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
    def agent_url(self) -> str:
        """The agent API's WebSocket URL, on this machine."""
        assert self._http is not None, "the hub isn't running"
        return f"ws://{self._host or 'localhost'}:{self._http.address[1]}/agent"

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
        sessions = Sessions()
        self._registry = Registry(
            StateStore(self._state_dir),
            udp_port=self.udp_port,
            timing=self.config.timing,
            auto_approve=self._auto_approve,
            presence=sessions,
        )
        self._core = Core(self._registry, sessions, self.config.agent_token)
        self._tcp = await loop.create_server(self._registry.connection, self._host, port)
        self._http = HttpServer(
            agent_app(self._core, self.config.timing),
            host=self._host,
            port=self.config.http_port,
            timing=self.config.timing,
        )
        await self._http.start()
        self._owner = OwnerServer(self._registry, self._runtime_dir)
        await self._owner.start()
        if self._mdns is not None:
            self._advertiser = Advertiser(self._mdns, self._host, self.device_address[1])
            self._advertiser.start()

    async def close(self) -> None:
        advertiser, owner, tcp = self._advertiser, self._owner, self._tcp
        udp, registry, core, http = self._udp, self._registry, self._core, self._http
        self._advertiser = self._owner = self._tcp = self._udp = self._registry = None
        self._core = self._http = None
        if advertiser is not None:
            await advertiser.close()
        if owner is not None:
            await owner.close()
        # The Session ends before the Connections close.
        if core is not None:
            await core.shutdown()
        if http is not None:
            await http.close()
        if tcp is not None:
            tcp.close()
        if udp is not None:
            udp.close()
        # The registry closes the Connections: uvloop's Server has no close_clients.
        if registry is not None:
            await registry.close()
        if tcp is not None:
            await tcp.wait_closed()
