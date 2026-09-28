"""The handshake and keepalive, driven by a hand-rolled device that can break the protocol."""

import json
from collections.abc import Awaitable, Callable

from tinyaisense_hub import Hub, OwnerClient, Timing
from tinyaisense_wire import (
    Error,
    Event,
    Frame,
    FrameType,
    Hello,
    Pending,
    Pong,
    Welcome,
    encode_frame,
)

from .raw_device import DEVICE_ID, RawDevice, echo_manifest

StartHub = Callable[..., Awaitable[Hub]]


async def test_welcome_carries_the_name_conn_id_udp_port_and_token(
    hub: Hub, owner: OwnerClient
) -> None:
    device = await RawDevice.connect(hub)
    device.send(Hello(None, 5000, echo_manifest()))
    assert await device.receive() == Pending()

    await owner.approve(DEVICE_ID, "kitchen")

    welcome = await device.receive()
    assert isinstance(welcome, Welcome)
    assert welcome.name == "kitchen"
    assert welcome.conn_id != 0
    assert welcome.udp_port == hub.udp_port
    assert welcome.token is not None


async def test_a_paired_device_is_welcomed_without_a_token(hub: Hub, owner: OwnerClient) -> None:
    first = await RawDevice.connect(hub)
    first.send(Hello(None, 5000, echo_manifest()))
    await first.receive()
    await owner.approve(DEVICE_ID)
    paired = await first.receive()
    assert isinstance(paired, Welcome)

    again = await RawDevice.connect(hub)
    again.send(Hello(paired.token, 5000, echo_manifest()))

    welcome = await again.receive()
    assert isinstance(welcome, Welcome)
    assert welcome.token is None


async def test_a_token_the_hub_did_not_issue_is_unauthorized(hub: Hub) -> None:
    device = await RawDevice.connect(hub)
    device.send(Hello("made-up", 5000, echo_manifest()))

    [error] = await device.until_closed()

    assert isinstance(error, Error)
    assert error.code == "unauthorized"


async def test_a_paired_device_with_the_wrong_token_is_unauthorized(
    hub: Hub, owner: OwnerClient
) -> None:
    first = await RawDevice.connect(hub)
    first.send(Hello(None, 5000, echo_manifest()))
    await first.receive()
    await owner.approve(DEVICE_ID)

    impostor = await RawDevice.connect(hub)
    impostor.send(Hello("made-up", 5000, echo_manifest()))

    [error] = await impostor.until_closed()
    assert isinstance(error, Error)
    assert error.code == "unauthorized"


async def test_another_protocol_major_gets_protocol_mismatch_with_the_hub_major(hub: Hub) -> None:
    hello = Hello(None, 5000, echo_manifest()).to_json()
    hello["manifest"]["protocol"] = 2
    device = await RawDevice.connect(hub)
    device.send_bytes(encode_frame(Frame(FrameType.JSON, json.dumps(hello).encode())))

    [error] = await device.until_closed()

    assert isinstance(error, Error)
    assert (error.code, error.protocol) == ("protocol_mismatch", 1)


async def test_a_connection_without_hello_is_closed(hub: Hub) -> None:
    device = await RawDevice.connect(hub)

    assert await device.until_closed() == []


async def test_only_ping_and_pong_flow_before_welcome(hub: Hub, owner: OwnerClient) -> None:
    device = await RawDevice.connect(hub)
    device.send(Hello(None, 5000, echo_manifest()))
    assert await device.receive() == Pending()

    device.send(Pong(1, 2))
    await owner.approve(DEVICE_ID)

    assert isinstance(await device.receive(), Welcome)


async def test_an_event_before_welcome_closes_the_connection(hub: Hub) -> None:
    device = await RawDevice.connect(hub)
    device.send(Hello(None, 5000, echo_manifest()))
    assert await device.receive() == Pending()

    device.send(Event(0, "button", "down", 1, {}))

    assert await device.until_closed() == []


async def test_a_second_hello_closes_the_connection(hub: Hub) -> None:
    device = await RawDevice.connect(hub)
    device.send(Hello(None, 5000, echo_manifest()))
    assert await device.receive() == Pending()

    device.send(Hello(None, 5000, echo_manifest()))

    assert await device.until_closed() == []


async def test_invalid_json_closes_the_connection(hub: Hub) -> None:
    device = await RawDevice.connect(hub)

    device.send_bytes(encode_frame(Frame(FrameType.JSON, b"{not json")))

    assert await device.until_closed() == []


async def test_an_oversized_frame_closes_the_connection(hub: Hub) -> None:
    device = await RawDevice.connect(hub)

    device.send_bytes(bytes([1, 0, 0x01, 0x10]))  # len 4097

    assert await device.until_closed() == []


async def test_default_names_stay_unique(start_hub: StartHub) -> None:
    hub = await start_hub(auto_approve=True)
    names: list[str] = []
    for device_id in ("02aaaaaa1234", "02bbbbbb1234"):
        device = await RawDevice.connect(hub)
        device.send(Hello(None, 5000, echo_manifest(device_id)))
        welcome = await device.receive()
        assert isinstance(welcome, Welcome)
        names.append(welcome.name)

    assert names == ["echo-1234", "echo-1234-2"]


async def test_a_new_hello_from_a_connected_device_supersedes_its_old_connection(
    hub: Hub,
) -> None:
    old = await RawDevice.connect(hub)
    old.send(Hello(None, 5000, echo_manifest()))
    assert await old.receive() == Pending()

    new = await RawDevice.connect(hub)
    new.send(Hello(None, 5000, echo_manifest()))

    assert await new.receive() == Pending()
    assert await old.until_closed() == []


async def test_the_hub_pings_a_device_from_its_hello_on(hub: Hub) -> None:
    device = await RawDevice.connect(hub)
    device.send(Hello(None, 5000, echo_manifest()))

    first = await device.receive_ping()
    device.send(Pong(first.t, 1))
    second = await device.receive_ping()

    assert second.t > first.t


async def test_a_device_that_answers_pings_stays_connected(start_hub: StartHub) -> None:
    hub = await start_hub(
        timing=Timing(hello_timeout_ms=300, ping_interval_ms=50, dead_after_ms=400)
    )
    device = await RawDevice.connect(hub)
    device.send(Hello(None, 5000, echo_manifest()))

    # Twenty pings, 50 ms apart, outlast the dead timeout more than twice over.
    for ts in range(20):
        ping = await device.receive_ping()
        device.send(Pong(ping.t, ts))

    assert await device.receive() == Pending()


async def test_a_silent_device_is_dropped(hub: Hub) -> None:
    device = await RawDevice.connect(hub)
    device.send(Hello(None, 5000, echo_manifest()))

    assert await device.until_closed() == [Pending()]
