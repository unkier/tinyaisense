"""mDNS: the hub advertises its device port, so that devices need no address."""

import asyncio
import contextlib
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from typing import cast

from tinyaisense_wire import PROTOCOL
from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

_MDNS_GROUP = ("224.0.0.251", 5353)

_log = logging.getLogger("tinyaisense_hub.discovery")


class Advertiser:
    """Advertises `service` with TXT `proto=<major>` for as long as it runs.

    Registration runs in the background: probing the name takes a second or so, and the
    device port serves meanwhile. mDNS trouble is logged, never fatal: devices set to a
    static hub address still work.
    """

    def __init__(self, service: str, host: str | None, port: int) -> None:
        """`host` is the address the hub serves devices on; all interfaces if None."""
        self._service = service
        self._host = host
        self._port = port
        self._zeroconf: AsyncZeroconf | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        address = self._address()
        if address is None:
            return
        try:
            self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
        except OSError as e:
            _log.warning("not advertising over mDNS: %s", e)
            return
        label = re.sub("[^a-z0-9-]+", "-", socket.gethostname().split(".")[0].lower()).strip("-")
        info = AsyncServiceInfo(
            self._service,
            f"tinyaisense hub on {label or 'localhost'}.{self._service}",
            port=self._port,
            properties={"proto": str(PROTOCOL)},
            # Our own host name, so as not to fight the system's mDNS responder over its own.
            server=f"tinyaisense-{label}-{self._port}.local.",
            addresses=[socket.inet_aton(address)],
        )
        self._task = asyncio.create_task(self._register(self._zeroconf, info, address))

    async def close(self) -> None:
        """Withdraw the advertisement."""
        task, zeroconf = self._task, self._zeroconf
        self._task = self._zeroconf = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if zeroconf is not None:
            await zeroconf.async_close()

    def _address(self) -> str | None:
        """The one IPv4 address to advertise, or None to not advertise."""
        if self._host is not None:
            try:
                return str(ipaddress.IPv4Address(self._host))
            except ValueError:
                _log.warning("not advertising over mDNS: %s isn't an IPv4 address", self._host)
                return None
        # Serving on every interface: advertise only the one mDNS itself goes out on. Other
        # interfaces, such as container bridges, are rarely reachable from a device.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            try:
                probe.connect(_MDNS_GROUP)
            except OSError as e:
                _log.warning("not advertising over mDNS: no route for it: %s", e)
                return None
            return probe.getsockname()[0]

    async def _register(
        self, zeroconf: AsyncZeroconf, info: AsyncServiceInfo, address: str
    ) -> None:
        try:
            # Probes the name, then returns the announcement to wait for.
            register = cast(
                Callable[..., Awaitable[Awaitable[None]]],
                zeroconf.async_register_service,  # pyright: ignore[reportUnknownMemberType]
            )
            await (await register(info, allow_name_change=True))
        except Exception as e:
            _log.warning("mDNS registration failed: %r", e)
        else:
            _log.info("advertising %s:%d over mDNS as %r", address, self._port, info.name)
