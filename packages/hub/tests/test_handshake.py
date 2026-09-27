"""The handshake, driven by a hand-rolled device that can break the protocol."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Self

from tinyaisense_hub import Hub, OwnerClient
from tinyaisense_sim import PROFILES
from tinyaisense_wire import (
    ControlDecoder,
    Error,
    Event,
    Frame,
    FrameType,
    Hello,
    Manifest,
    Message,
    Pending,
    Pong,
    Welcome,
    encode_control,
    encode_frame,
)

DEVICE_ID = "02aabbccddee"

StartHub = Callable[..., Awaitable[Hub]]


def echo_manifest(device_id: str = DEVICE_ID) -> Manifest:
    return PROFILES["m5stack-atom-echo"](device_id, "raw-1")


class RawDevice:
    """A device's TCP side, one message at a time."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._decoder = ControlDecoder()
        self._inbox: list[Message] = []

    @classmethod
    async def connect(cls, hub: Hub) -> Self:
        return cls(*await asyncio.open_connection(*hub.device_address))

    def send(self, message: Message) -> None:
        self._writer.write(encode_control(message))

    def send_bytes(self, data: bytes) -> None:
        self._writer.write(data)

    async def receive(self) -> Message:
        while not self._inbox:
            data = await self._reader.read(65536)
            assert data, "the hub closed the Connection"
            self._inbox.extend(self._decoder.feed(data))
        return self._inbox.pop(0)

    async def until_closed(self) -> list[Message]:
        """Everything the hub sends until it closes the Connection."""
        while data := await self._reader.read(65536):
            self._inbox.extend(self._decoder.feed(data))
        messages, self._inbox = self._inbox, []
        return messages


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
