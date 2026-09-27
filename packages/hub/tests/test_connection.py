"""Connection resilience: presence, dead Connections, reconnects and the Simulator's TCP faults."""

import asyncio
import contextlib
import secrets
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from tinyaisense_hub import Hub, OwnerClient, Timing
from tinyaisense_sim import SimDevice, Simulator
from tinyaisense_sim.pytest_plugin import SimulatorFactory
from tinyaisense_wire import MDNS_SERVICE, JsonObject
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

ECHO = "m5stack-atom-echo"
NEVER_DEAD = Timing(hello_timeout_ms=300, ping_interval_ms=100, dead_after_ms=60_000)
"""A hub that only notices a drop the device side tells it of."""

StartHub = Callable[..., Awaitable[Hub]]
"""The `start_hub` fixture."""


def now_ms() -> int:
    return time.time_ns() // 1_000_000


async def devices_when(
    owner: OwnerClient, predicate: Callable[[list[JsonObject]], bool]
) -> list[JsonObject]:
    """Poll the owner's `devices` until `predicate` holds for it."""
    while not predicate(devices := await owner.devices()):  # noqa: ASYNC110 - no change events
        await asyncio.sleep(0.01)
    return devices


async def paired(sim: Simulator, owner: OwnerClient, name: str = "kitchen") -> SimDevice:
    device = await sim.add(ECHO, name)
    await owner.approve(device.device_id, name)
    await device.wait_for(lambda: device.state == "online")
    return device


async def reconnected(device: SimDevice) -> None:
    """Wait until `device` is back online on its second Connection."""
    await device.wait_for(lambda: device.reconnects == 1 and device.state == "online")


def offline(devices: list[JsonObject]) -> bool:
    return not devices[0]["online"]


async def test_devices_shows_who_is_online_and_when_each_was_last_seen(
    sim: Simulator, owner: OwnerClient
) -> None:
    start = now_ms()
    device = await paired(sim, owner)
    [online] = await owner.devices()
    assert online["online"] is True
    assert start <= online["last_seen"] <= now_ms()

    await device.close()

    [gone] = await devices_when(owner, offline)
    assert start <= gone["last_seen"] <= now_ms()


async def test_a_pending_device_is_online(sim: Simulator, owner: OwnerClient) -> None:
    await sim.add(ECHO, "kitchen")

    [pending] = await owner.devices()

    assert (pending["pending"], pending["online"]) == (True, True)


@pytest.fixture
async def owners() -> AsyncIterator[Callable[[Hub], Awaitable[OwnerClient]]]:
    """`await owners(hub)` is an owner client of `hub`, closed at the end of the test."""
    async with contextlib.AsyncExitStack() as stack:

        async def connect(hub: Hub) -> OwnerClient:
            return await stack.enter_async_context(OwnerClient(hub.owner_socket))

        yield connect


async def test_rst_fault_the_hub_drops_the_connection_at_once(
    start_hub: StartHub,
    simulator: SimulatorFactory,
    owners: Callable[[Hub], Awaitable[OwnerClient]],
) -> None:
    hub = await start_hub(timing=NEVER_DEAD)
    owner = await owners(hub)
    device = await paired(simulator(hub.device_address), owner)
    conn_id = device.conn_id

    device.faults.rst()

    await devices_when(owner, offline)
    await reconnected(device)
    assert device.conn_id not in (None, conn_id)  # a fresh Connection: no resume


async def test_close_fault_the_hub_drops_the_connection_at_once(
    start_hub: StartHub,
    simulator: SimulatorFactory,
    owners: Callable[[Hub], Awaitable[OwnerClient]],
) -> None:
    hub = await start_hub(timing=NEVER_DEAD)
    owner = await owners(hub)
    device = await paired(simulator(hub.device_address), owner)

    device.faults.close()

    await devices_when(owner, offline)
    await reconnected(device)


async def test_vanish_fault_the_hub_declares_the_silent_connection_dead(
    hub: Hub, simulator: SimulatorFactory, owner: OwnerClient
) -> None:
    # The device waits longer than the hub before it gives up on the Connection.
    device = await paired(simulator(hub.device_address, dead_after_ms=2000), owner)

    device.faults.vanish()

    await devices_when(owner, offline)
    await reconnected(device)


async def test_stall_reads_fault_the_hub_declares_a_device_that_stops_reading_dead(
    sim: Simulator, owner: OwnerClient
) -> None:
    device = await paired(sim, owner)

    device.faults.stall_reads = True

    await devices_when(owner, offline)
    device.faults.stall_reads = False
    await reconnected(device)


async def test_a_device_that_gives_up_on_a_vanished_connection_supersedes_it(
    start_hub: StartHub,
    simulator: SimulatorFactory,
    owners: Callable[[Hub], Awaitable[OwnerClient]],
) -> None:
    hub = await start_hub(timing=NEVER_DEAD)
    owner = await owners(hub)
    device = await paired(simulator(hub.device_address, dead_after_ms=300), owner)

    device.faults.vanish()

    # The hub still holds the old Connection when the new hello arrives.
    await reconnected(device)
    assert [(d["name"], d["online"]) for d in await owner.devices()] == [("kitchen", True)]


async def test_devices_reconnect_by_themselves_after_a_hub_restart(
    start_hub: StartHub,
    simulator: SimulatorFactory,
    owners: Callable[[Hub], Awaitable[OwnerClient]],
) -> None:
    hub = await start_hub()
    port = hub.device_address[1]
    sim = simulator(hub.device_address)
    owner = await owners(hub)
    kitchen = await paired(sim, owner, "kitchen")
    den = await paired(sim, owner, "den")

    await hub.close()
    await start_hub(device_port=port)

    for device in (kitchen, den):
        await reconnected(device)


async def mdns_hub(zeroconf: AsyncZeroconf, service: str, port: int) -> AsyncServiceInfo:
    """Browse `service` until the hub on `port` shows up; other hubs may be about."""
    names: asyncio.Queue[str] = asyncio.Queue()

    def changed(name: str, state_change: ServiceStateChange, **_: object) -> None:
        if state_change is not ServiceStateChange.Removed:
            names.put_nowait(name)

    browser = AsyncServiceBrowser(zeroconf.zeroconf, service, handlers=[changed])
    try:
        while True:
            info = AsyncServiceInfo(service, await names.get())
            if await info.async_request(zeroconf.zeroconf, 3000) and info.port == port:
                return info
    finally:
        await browser.async_cancel()


def private_service() -> str:
    """An mDNS service type of this test's own, so that it finds no other hub on the LAN."""
    return f"_tas{secrets.token_hex(3)}._tcp.local."


def test_hubs_advertise_the_service_type_of_the_protocol() -> None:
    assert MDNS_SERVICE == "_tinyaisense._tcp.local."


async def test_the_hub_advertises_itself_over_mdns_with_its_protocol_major(
    start_hub: StartHub,
) -> None:
    service = private_service()
    hub = await start_hub(mdns=service)

    zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
    try:
        async with asyncio.timeout(10):
            info = await mdns_hub(zeroconf, service, hub.device_address[1])
    finally:
        await zeroconf.async_close()

    assert info.properties == {b"proto": b"1"}
    assert info.parsed_addresses() == ["127.0.0.1"]


async def test_a_simulator_finds_the_hub_over_mdns_by_default(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    service = private_service()
    await start_hub(auto_approve=True, mdns=service)

    async with asyncio.timeout(10):
        device = await simulator(mdns_service=service).add(ECHO, "kitchen")

    assert device.state == "online"


async def test_a_simulator_skips_a_hub_of_another_protocol_major(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    service = private_service()
    zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
    try:
        future = AsyncServiceInfo(
            service,
            f"future hub.{service}",
            port=9,  # discard: nothing listens there
            properties={"proto": "2"},
            server="tinyaisense-future.local.",
            addresses=[socket.inet_aton("127.0.0.1")],
        )
        await (await zeroconf.async_register_service(future))  # pyright: ignore[reportUnknownMemberType]
        await start_hub(auto_approve=True, mdns=service)

        async with asyncio.timeout(10):
            device = await simulator(mdns_service=service).add(ECHO, "kitchen")

        assert device.state == "online"
    finally:
        await zeroconf.async_close()


async def test_after_repeated_failures_a_device_resolves_the_hub_again(
    start_hub: StartHub, simulator: SimulatorFactory
) -> None:
    service = private_service()
    hub = await start_hub(auto_approve=True, mdns=service)
    async with asyncio.timeout(10):
        device = await simulator(mdns_service=service).add(ECHO, "kitchen")

    await hub.close()
    await start_hub(mdns=service)  # on another port

    async with asyncio.timeout(15):
        await reconnected(device)
