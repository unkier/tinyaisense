"""The agent API: Sessions and device presence, driven through the SDK client."""

import asyncio
import json
import time
from pathlib import Path

import pytest
import tinyaisense
import websockets
from tinyaisense_hub import Hub, OwnerClient, SpeechConfig, Timing
from tinyaisense_hub.control import Control
from tinyaisense_hub.core import Core
from tinyaisense_hub.devices import Registry
from tinyaisense_hub.errors import HubError
from tinyaisense_hub.session import EndReason, Sessions, UtteranceOptIn
from tinyaisense_hub.speech.layer import Speech
from tinyaisense_hub.state import StateStore
from tinyaisense_sim import SimDevice, Simulator
from tinyaisense_wire import JsonObject
from websockets.asyncio.client import ClientConnection

from .conftest import FAST_TIMING

ECHO = "m5stack-atom-echo"
TOKEN = "agent-token"
"""The `start_hub` fixture's agent token."""


def now_ms() -> int:
    return time.time_ns() // 1_000_000


async def test_an_agent_claims_the_session_with_the_agent_token(hub: Hub) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN) as session:
        assert session.id


async def test_a_wrong_token_is_refused(hub: Hub) -> None:
    with pytest.raises(tinyaisense.ApiError) as refused:
        await tinyaisense.connect(hub.agent_url, "not-the-token")

    assert refused.value.code == "unauthorized"


async def test_a_second_agent_gets_busy_with_who_holds_the_hub_since_when(hub: Hub) -> None:
    start = now_ms()
    async with tinyaisense.connect(hub.agent_url, TOKEN):
        with pytest.raises(tinyaisense.ApiError) as refused:
            await tinyaisense.connect(hub.agent_url, TOKEN)

    assert refused.value.code == "busy"
    assert refused.value.data["holder"] == "agent_api"
    assert start <= refused.value.data["since"] <= now_ms()


async def test_takeover_ends_the_old_session_and_claims_the_hub(hub: Hub) -> None:
    async with (
        tinyaisense.connect(hub.agent_url, TOKEN) as old,
        tinyaisense.connect(hub.agent_url, TOKEN, takeover=True) as new,
    ):
        ended = [(n.method, n.params["reason"]) async for n in old.notifications()]

        assert ended == [("session.ended", "taken_over")]
        assert old.ended == "taken_over"
        with pytest.raises(tinyaisense.SessionEnded):
            await old.devices()
        assert await new.devices() == []


async def test_the_session_ends_when_its_link_closes(hub: Hub) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN) as first:
        pass

    async with tinyaisense.connect(hub.agent_url, TOKEN) as second:
        assert second.id != first.id


async def test_the_session_ends_with_shutdown_when_the_hub_closes(hub: Hub) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        await hub.close()

        ended = [(n.method, n.params["reason"]) async for n in agent.notifications()]

    assert ended == [("session.ended", "shutdown")]


class NullSink:
    def notify(self, method: str, params: JsonObject) -> None:
        pass

    def ended(self, reason: EndReason) -> None:
        pass

    def audio(self, handle: int, index: int, pcm: bytes) -> None:
        pass

    def audio_ended(self, handle: int) -> int:
        return 0


async def test_no_session_starts_once_the_hub_is_shutting_down(tmp_path: Path) -> None:
    # The Session ends before the links close, so a hello can land in between.
    udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
    )
    control = Control(Timing())
    sessions = Sessions(control)
    registry = Registry(
        StateStore(tmp_path), udp=udp, timing=Timing(), auto_approve=False, observer=control
    )
    core = Core(registry, sessions, control, Speech(SpeechConfig(), control), TOKEN)

    await core.shutdown()
    udp.close()

    with pytest.raises(HubError) as refused:
        await core.open_session(
            TOKEN, "agent_api", NullSink(), takeover=True, utterances=UtteranceOptIn()
        )
    assert refused.value.code == "failed"


async def paired(sim: Simulator, owner: OwnerClient, name: str) -> SimDevice:
    device = await sim.add(ECHO, name)
    await owner.approve(device.device_id, name)
    await device.wait_for(lambda: device.state == "online")
    return device


async def test_devices_lists_paired_devices_with_their_manifests_but_not_pending_ones(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner, "kitchen")
    await sim.add(ECHO, "stranger")

    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        devices = await agent.devices()

    assert devices == [{"name": "kitchen", "online": True, "manifest": kitchen.manifest.to_json()}]
    assert devices[0]["manifest"]["exclusive"] == [["mic", "speaker"]]


async def test_the_agent_hears_devices_come_online_and_go_offline(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        start = now_ms()

        kitchen = await paired(sim, owner, "kitchen")
        online = await anext(notifications)
        await kitchen.close()
        offline = await anext(notifications)

        assert online.method == "device.online"
        assert online.params == {
            "device": "kitchen",
            "manifest": kitchen.manifest.to_json(),
            "t": online.t,
        }
        assert offline.method == "device.offline"
        assert offline.params == {"device": "kitchen", "t": offline.t}
        assert start <= online.t <= offline.t <= now_ms()


async def test_devices_lists_an_offline_device_with_its_last_manifest(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        kitchen = await paired(sim, owner, "kitchen")
        await kitchen.close()
        while (await anext(notifications)).method != "device.offline":
            pass

        devices = await agent.devices()

    assert devices == [{"name": "kitchen", "online": False, "manifest": kitchen.manifest.to_json()}]


async def test_a_renamed_device_goes_offline_by_its_old_name_and_online_by_the_new(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    await paired(sim, owner, "kitchen")
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()

        await owner.rename("kitchen", "den")

        assert [
            (n.method, n.params["device"]) for n in [await anext(notifications) for _ in range(2)]
        ] == [("device.offline", "kitchen"), ("device.online", "den")]


async def test_an_unknown_method_is_unsupported(hub: Hub) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.call("teleport")

    assert refused.value.code == "unsupported"


async def test_an_unknown_param_is_invalid(hub: Hub) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.call("devices", online_only=True)

    assert refused.value.code == "invalid_args"


async def test_a_second_hello_on_a_link_is_invalid(hub: Hub) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.call("hello", token=TOKEN)

        assert refused.value.code == "invalid_args"
        assert await agent.devices() == []


# The wire itself, below the SDK.


async def raw(hub: Hub) -> ClientConnection:
    return await websockets.connect(hub.agent_url, ping_interval=None)


async def rpc(ws: ClientConnection, request: object) -> JsonObject:
    """Send `request`, as JSON unless it's text already, and return the reply."""
    await ws.send(request if isinstance(request, str) else json.dumps(request))
    return json.loads(await ws.recv())


HELLO = {"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {"token": TOKEN}}


async def test_a_link_must_start_with_hello(hub: Hub) -> None:
    async with await raw(hub) as ws:
        reply = await rpc(ws, {"jsonrpc": "2.0", "id": 1, "method": "devices"})

        assert reply["id"] == 1
        assert reply["error"]["data"]["code"] == "unauthorized"
        await ws.wait_closed()


async def test_a_notification_before_hello_is_ignored(hub: Hub) -> None:
    async with await raw(hub) as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "hi"}))

        reply = await rpc(ws, HELLO)

    assert "session" in reply["result"]


async def test_bad_hello_params_are_invalid_params(hub: Hub) -> None:
    async with await raw(hub) as ws:
        reply = await rpc(ws, HELLO | {"params": {"token": TOKEN, "bogus": 1}})

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["code"] == "invalid_args"


async def test_a_link_without_hello_is_closed(hub: Hub) -> None:
    async with await raw(hub) as ws:
        await ws.wait_closed()  # after FAST_TIMING.hello_timeout_ms

        assert ws.close_code == 1008


async def test_invalid_json_gets_a_parse_error(hub: Hub) -> None:
    async with await raw(hub) as ws:
        await rpc(ws, HELLO)

        reply = await rpc(ws, "{not json")
        batch = await rpc(ws, [{"jsonrpc": "2.0", "id": 2, "method": "devices"}])

    assert (reply["id"], reply["error"]["code"]) == (None, -32700)
    assert reply["error"]["data"]["code"] == "invalid_args"
    assert (batch["id"], batch["error"]["code"]) == (None, -32600)


async def test_an_idle_agent_that_answers_pings_keeps_the_session(hub: Hub) -> None:
    async with await raw(hub) as ws:  # it never pings, but its library answers the hub's
        await rpc(ws, HELLO)
        await asyncio.sleep(2 * FAST_TIMING.agent_dead_after_ms / 1000)

        reply = await rpc(ws, {"jsonrpc": "2.0", "id": 2, "method": "devices"})

    assert reply["result"] == {"devices": []}


async def test_a_dead_agent_loses_the_session_after_missed_pings(hub: Hub) -> None:
    async with await raw(hub) as ws:
        await rpc(ws, HELLO)
        ws.transport.pause_reading()  # it stops answering the hub's pings
        start = time.monotonic()

        while True:
            try:
                async with tinyaisense.connect(hub.agent_url, TOKEN):
                    break
            except tinyaisense.ApiError as e:
                if e.code != "busy":
                    raise
                await asyncio.sleep(0.05)
        elapsed = time.monotonic() - start
        ws.transport.abort()  # a close handshake would wait for a reply it can't read

    assert elapsed >= FAST_TIMING.agent_dead_after_ms / 1000
