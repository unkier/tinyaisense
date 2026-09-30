"""The MCP server at `/mcp`, driven through the MCP SDK client at both protocol versions."""

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Any, cast

import httpx2
import pytest
import tinyaisense
from agents.mcp import MCPServerStreamableHttp
from mcp import Client
from mcp.client.client import ConnectMode
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import CallToolResult, Implementation, TextContent
from tinyaisense_hub import Hub, OwnerClient, SpeechConfig, Timing
from tinyaisense_hub.alarm import now_us
from tinyaisense_hub.control import Control
from tinyaisense_hub.keepalive import Tick
from tinyaisense_hub.mcp_events import Inbox
from tinyaisense_hub.session import Sessions
from tinyaisense_hub.speech import fake
from tinyaisense_hub.speech.layer import Speech
from tinyaisense_sim import SimDevice, Simulator, sources
from tinyaisense_sim.led import Light
from tinyaisense_wire import JsonObject

from .conftest import StartHub
from .test_listen import SPEECH, TIMING, paired, says

TOKEN = "agent-token"
"""The `start_hub` fixture's agent token."""
VERSIONS = ["2025-11-25", "2026-07-28"]
MODES: dict[str, ConnectMode] = {"2025-11-25": "legacy", "2026-07-28": "auto"}
"""How the client reaches each version: the initialize handshake, or `server/discover`,
which picks the newest."""


@pytest.fixture(params=VERSIONS)
def version(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
async def hub(start_hub: StartHub, request: pytest.FixtureRequest) -> Hub:
    """With the fake speech plugin: it speaks and hears. Parametrize it with a `Timing`."""
    return await start_hub(speech=SPEECH, timing=getattr(request, "param", TIMING))


@contextlib.asynccontextmanager
async def mcp(
    hub: Hub, version: str, *, token: str = TOKEN, name: str = "test-agent"
) -> AsyncGenerator[Client]:
    """An MCP client of `hub`, speaking `version`. Leaving it ends its MCP session, on the
    handshake versions."""
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        create_mcp_http_client(headers) as http,
        Client(
            streamable_http_client(hub.mcp_url, http_client=http),
            mode=MODES[version],
            client_info=Implementation(name=name, version="1.0"),
        ) as client,
    ):
        assert client.protocol_version == version
        yield client


async def test_an_mcp_client_with_the_agent_token_lists_the_static_tools(
    hub: Hub, version: str
) -> None:
    async with mcp(hub, version) as client:
        tools = (await client.list_tools()).tools
        capabilities = client.server_capabilities

    assert sorted(tool.name for tool in tools) == [
        "command",
        "devices",
        "led_release",
        "listen",
        "say",
        "set_reflex",
        "voices",
        "wait_for_event",
    ]
    assert all(tool.output_schema is not None for tool in tools)
    assert capabilities.tools is not None
    assert not capabilities.tools.list_changed
    assert capabilities.resources is None
    assert capabilities.prompts is None


async def call(
    client: Client, tool: str, args: JsonObject | None = None, *, fails: bool = False
) -> JsonObject:
    """Call `tool` and return its structured result, which must be its text too. Raises
    `ToolError` if it failed, unless it `fails` as expected, but with another code than
    `busy`."""
    result = await client.call_tool(tool, args or {})
    structured = checked(result)
    if fails:
        assert result.is_error
        assert structured["code"] != "busy"
    elif result.is_error:
        raise ToolError(structured)
    return structured


def checked(result: CallToolResult) -> JsonObject:
    """The structured result, once checked against its text."""
    structured = cast(JsonObject, result.structured_content)
    assert isinstance(structured, dict)
    [text] = result.content
    assert isinstance(text, TextContent)
    assert json.loads(text.text) == structured
    return structured


class ToolError(Exception):
    def __init__(self, error: JsonObject) -> None:
        super().__init__(error)
        self.code: str = error["code"]
        self.error = error


async def test_a_request_without_the_agent_token_is_refused(hub: Hub) -> None:
    initialize: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "stranger", "version": "1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    async with httpx2.AsyncClient() as http:
        without = await http.post(hub.mcp_url, json=initialize, headers=headers)
        wrong = await http.post(
            hub.mcp_url, json=initialize, headers=headers | {"Authorization": "Bearer nope"}
        )

    for refused in (without, wrong):
        assert refused.status_code == 401
        assert refused.headers["WWW-Authenticate"].startswith("Bearer")


async def test_a_host_header_not_in_allowed_hosts_is_refused(hub: Hub) -> None:
    async with httpx2.AsyncClient() as http:
        refused = await http.post(
            hub.mcp_url,
            json={},
            headers={"Authorization": f"Bearer {TOKEN}", "Host": "evil.example:80"},
        )

    assert refused.status_code == 421


async def test_the_hub_serves_mcp_by_the_host_names_of_its_config(start_hub: StartHub) -> None:
    hub = await start_hub(allowed_hosts=("hub.local:*",))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    local = {"Host": "localhost:80", **headers}
    named = {"Host": "hub.local:8421", "Origin": "http://hub.local:8421", **headers}
    async with httpx2.AsyncClient() as http:
        refused = await http.post(hub.mcp_url, json={}, headers=local)
        taken = await http.post(hub.mcp_url, json={}, headers=named)

    assert refused.status_code == 421
    assert taken.status_code == 400  # past the host check, to the JSON-RPC one


async def test_devices_lists_each_capability_by_address_with_the_commands_agents_may_send(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    await paired(sim, owner, "kitchen")
    await sim.add("m5stack-atom-echo", "stranger")  # pending: hidden

    async with mcp(hub, version) as client:
        devices = await call(client, "devices")

    [kitchen] = devices["devices"]
    assert kitchen["name"] == "kitchen"
    assert kitchen["online"] is True
    assert kitchen["board"] == "m5stack-atom-echo"
    assert kitchen["exclusive"] == [["kitchen/mic", "kitchen/speaker"]]
    listed = {c["address"]: (c["kind"], sorted(c["commands"])) for c in kitchen["capabilities"]}
    assert listed == {
        "kitchen/mic": ("mic", []),
        "kitchen/speaker": ("speaker", []),
        "kitchen/button": ("button", []),
        "kitchen/led": ("led", ["off", "set"]),  # not the hub-only release
    }
    [led] = [c for c in kitchen["capabilities"] if c["kind"] == "led"]
    assert led["params"] == {"count": 1}
    assert led["commands"]["set"]["required"] == ["color"]


async def test_voices_lists_the_voices_by_id(hub: Hub, version: str) -> None:
    async with mcp(hub, version) as client:
        voices = await call(client, "voices")

    assert voices == {
        "voices": [
            {"id": "alto", "lang": "en", "name": "Alto"},
            {"id": "tenor", "lang": "en", "name": "Tenor"},
            {"id": "masha", "lang": "ru", "name": "Маша"},
        ]
    }


async def test_say_speaks_on_the_one_online_speaker_and_returns_once_it_has_played(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    kitchen = await paired(sim, owner)

    async with mcp(hub, version) as client:
        result = await call(client, "say", {"text": "Hello there."})

    assert result == {
        "reason": "drained",
        "played_ms": 120,
        "interrupted": False,
        "heard_text": "Hello there.",
    }
    assert fake.decode(kitchen.speaker.pcm()).text == "Hello there."


async def test_say_takes_an_address_a_voice_and_replace(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    await paired(sim, owner, "attic")
    kitchen = await paired(sim, owner, "kitchen")
    args = {"address": "kitchen/speaker", "voice": "masha", "replace": True}

    async with mcp(hub, version) as client:
        result = await call(client, "say", args | {"text": "Привет."})

    assert result["heard_text"] == "Привет."
    assert fake.decode(kitchen.speaker.pcm()).text == "Привет."
    assert fake.decode(kitchen.speaker.pcm()).registers == {2}  # masha's


async def test_listen_returns_the_final_transcript_its_lang_and_why_it_ended(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("Hello there, how are you?"))

    async with mcp(hub, version) as client:
        result = await call(client, "listen", {"lang": "en"})

    assert result | {"utterance_id": 0, "stats": {}} == {
        "utterance_id": 0,
        "reason": "end_of_turn",
        "transcript": "Hello there, how are you?",
        "lang": "en",
        "stats": {},
    }


async def test_say_with_then_listen_returns_what_the_human_said_once_it_has_played(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("Hello there, how are you?"))

    async with mcp(hub, version) as client:
        result = await call(client, "say", {"text": "Yes?", "then_listen": True})

    assert result == {
        "reason": "drained",
        "played_ms": 40,
        "interrupted": False,
        "heard_text": "Yes?",
        "utterance": {"text": "Hello there, how are you?", "lang": "en", "reason": "end_of_turn"},
    }


async def test_an_omitted_address_that_several_devices_fit_is_invalid_with_the_candidates(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    await paired(sim, owner, "attic")
    await paired(sim, owner, "kitchen")

    async with mcp(hub, version) as client:
        with pytest.raises(ToolError) as refused:
            await call(client, "say", {"text": "Hi."})

    assert refused.value.code == "invalid_args"
    assert refused.value.error["candidates"] == ["attic/speaker", "kitchen/speaker"]


async def test_an_omitted_address_goes_by_the_devices_online(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    async with mcp(hub, version) as client:
        with pytest.raises(ToolError) as none_paired:
            await call(client, "listen")
        attic = await paired(sim, owner, "attic")
        kitchen = await paired(sim, owner, "kitchen")
        await attic.close()
        await hub_sees_offline(client, "attic")
        kitchen.mic.feed(says("Hello there, how are you?"))
        heard = await call(client, "listen")
        await kitchen.close()
        await hub_sees_offline(client, "kitchen")
        with pytest.raises(ToolError) as none_online:
            await call(client, "listen")

    assert none_paired.value.code == "unknown_address"
    assert heard["transcript"] == "Hello there, how are you?"
    assert none_online.value.code == "disconnected"


async def hub_sees_offline(client: Client, name: str) -> None:
    """Wait until the hub lists device `name` as offline."""
    for _ in range(500):
        devices = (await call(client, "devices"))["devices"]
        if not next(d for d in devices if d["name"] == name)["online"]:
            return
    raise AssertionError(f"{name} is still online")


async def test_command_drives_the_led_until_led_release_hands_it_back(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    kitchen = await paired(sim, owner)

    async with mcp(hub, version) as client:
        result = await call(
            client,
            "command",
            {"address": "kitchen/led", "name": "set", "args": {"color": "#FF0000"}},
        )
        red = kitchen.lights()
        await call(client, "led_release", {"device": "kitchen"})
        released = kitchen.lights()

    assert result == {}
    assert red == (Light("#ff0000", 64, "solid", 1000),)
    assert released == (Light("#000000", 0),)  # the Status `agent`


async def test_set_reflex_disarms_and_arms_a_devices_button(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    kitchen = await paired(sim, owner)

    async with mcp(hub, version) as client:
        await call(client, "set_reflex", {"device": "kitchen", "armed": False})
        disarmed = kitchen.armed
        await call(client, "set_reflex", {"device": "kitchen", "armed": True})
        armed = kitchen.armed

    assert (disarmed, armed) == (False, True)


async def test_a_failure_is_an_error_result_with_its_code_and_message(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    await paired(sim, owner)

    async with mcp(hub, version) as client:
        unknown = await client.call_tool("led_release", {"device": "attic"})
        hub_only = await client.call_tool(
            "command", {"address": "kitchen/led", "name": "release", "args": {}}
        )
        bad_args = await client.call_tool("say", {"text": 5})
        unknown_arg = await client.call_tool("voices", {"verbose": True})

    for result, code in [
        (unknown, "unknown_address"),
        (hub_only, "unsupported"),
        (bad_args, "invalid_args"),
        (unknown_arg, "invalid_args"),
    ]:
        assert result.is_error
        error = checked(result)
        assert error["code"] == code
        assert error["message"]


async def test_the_server_instructions_open_with_a_paragraph_that_stands_alone(
    hub: Hub, version: str
) -> None:
    async with mcp(hub, version) as client:
        instructions = client.instructions

    assert instructions is not None
    assert len(instructions) < 2048
    first, _, rest = instructions.partition("\n\n")
    assert len(first) <= 512
    assert rest
    for topic in ("listen", "say", "half-duplex", "events", "interrupted", "wait_for_event"):
        assert topic in instructions


IDLE_MS = 300


def now_ms() -> int:
    return time.time_ns() // 1_000_000


async def test_the_first_tool_call_that_acts_claims_the_hub_for_the_mcp_client(
    hub: Hub, version: str
) -> None:
    async with mcp(hub, version) as client:
        await call(client, "devices")
        await call(client, "voices")
        async with tinyaisense.connect(hub.agent_url, TOKEN):
            pass  # devices and voices claim nothing
        start = now_ms()
        with pytest.raises(ToolError):
            await call(client, "led_release", {"device": "attic"})
        with pytest.raises(tinyaisense.ApiError) as refused:
            await tinyaisense.connect(hub.agent_url, TOKEN)

    assert refused.value.code == "busy"
    assert refused.value.data["holder"] == "mcp"
    assert start <= refused.value.data["since"] <= now_ms()


async def test_a_tool_call_gets_busy_with_the_holder_and_since_when_another_agent_holds_the_hub(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    await paired(sim, owner)
    start = now_ms()
    async with (
        tinyaisense.connect(hub.agent_url, TOKEN),
        mcp(hub, version) as client,
    ):
        devices = await call(client, "devices")
        with pytest.raises(ToolError) as refused:
            await call(client, "say", {"text": "Hello there."})

    assert devices["devices"][0]["name"] == "kitchen"
    assert refused.value.code == "busy"
    assert refused.value.error["holder"] == "agent_api"
    assert start <= refused.value.error["since"] <= now_ms()


async def test_another_mcp_client_gets_busy_and_cant_take_the_hub_over(
    hub: Hub, version: str
) -> None:
    async with mcp(hub, version, name="claude") as first, mcp(hub, version, name="codex") as second:
        await call(first, "led_release", {"device": "attic"}, fails=True)
        with pytest.raises(ToolError) as refused:
            await call(second, "led_release", {"device": "attic"})
        await call(first, "led_release", {"device": "attic"}, fails=True)

    assert refused.value.code == "busy"
    assert refused.value.error["holder"] == "mcp"


async def test_an_agent_that_takes_the_hub_over_ends_the_mcp_only_session(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    kitchen = await paired(sim, owner)
    async with mcp(hub, version) as client:
        await call(client, "set_reflex", {"device": "kitchen", "armed": False})
        async with tinyaisense.connect(hub.agent_url, TOKEN, takeover=True):
            await kitchen.wait_for(lambda: kitchen.armed)  # the new Session's default
            with pytest.raises(ToolError) as refused:
                await call(client, "set_reflex", {"device": "kitchen", "armed": False})

    assert refused.value.error["holder"] == "agent_api"


@pytest.mark.parametrize("hub", [replace(TIMING, mcp_idle_ms=IDLE_MS)], indirect=True)
async def test_an_mcp_only_session_is_released_once_no_call_has_run_for_the_idle_time(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("Hello there, how are you?", silence_ms=2 * IDLE_MS))
    async with mcp(hub, version) as client:
        # A call that runs longer than the idle time keeps the Session.
        listening = asyncio.create_task(call(client, "listen"))
        await kitchen.wait_for(lambda: kitchen.status == "listening")
        await asyncio.sleep(2 * IDLE_MS / 1000)
        with pytest.raises(tinyaisense.ApiError) as busy:
            await tinyaisense.connect(hub.agent_url, TOKEN)
        heard = await listening
        async with await claimed(hub):
            pass
        instructions = client.instructions

    assert instructions is not None
    assert "released after 0.3 s with no call" in instructions
    assert busy.value.code == "busy"
    assert heard["transcript"] == "Hello there, how are you?"


async def claimed(hub: Hub) -> tinyaisense.Session:
    """An agent-API Session, once the hub is free."""
    async with asyncio.timeout(5):
        while True:
            try:
                return await tinyaisense.connect(hub.agent_url, TOKEN)
            except tinyaisense.ApiError as e:
                if e.code != "busy":
                    raise
            await asyncio.sleep(0.05)


async def test_ending_the_mcp_session_releases_the_hub_on_the_handshake_versions(
    hub: Hub,
) -> None:
    async with mcp(hub, "2025-11-25") as client:
        await call(client, "led_release", {"device": "attic"}, fails=True)

    async with tinyaisense.connect(hub.agent_url, TOKEN):
        pass


async def test_a_2026_client_holds_the_hub_by_its_name_and_address_across_connections(
    hub: Hub,
) -> None:
    async with mcp(hub, "2026-07-28", name="claude") as client:
        await call(client, "led_release", {"device": "attic"}, fails=True)
    async with mcp(hub, "2026-07-28", name="claude") as again:
        await call(again, "led_release", {"device": "attic"}, fails=True)
    async with mcp(hub, "2026-07-28", name="codex") as other:
        with pytest.raises(ToolError) as refused:
            await call(other, "led_release", {"device": "attic"})

    assert refused.value.code == "busy"


async def test_the_openai_agents_sdk_client_lists_and_calls_the_tools(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    server = MCPServerStreamableHttp(
        {"url": hub.mcp_url, "headers": {"Authorization": f"Bearer {TOKEN}"}},
        client_session_timeout_seconds=60,
        use_structured_content=True,
    )
    kitchen.mic.feed(says("Hello there, how are you?"))
    async with server:
        tools = await server.list_tools()
        result = await server.call_tool("say", {"text": "Hello there.", "then_listen": True})

    assert "say" in [tool.name for tool in tools]
    said = checked(result)
    assert said["reason"] == "drained"
    assert said["utterance"]["text"] == "Hello there, how are you?"
    assert kitchen.speaker.pcm()


async def test_a_call_that_comes_as_the_session_goes_idle_gets_a_session_that_lasts() -> None:
    control = Control(Timing())
    sessions = Sessions(control, Speech(SpeechConfig(), control, Timing()), Timing())
    async with sessions.mcp_call("claude", Inbox) as idle:
        pass
    sessions.mcp_idle(idle, Tick(), now_us() + 10**12)  # its idle timer fires

    # Before the Session's end runs.
    async with sessions.mcp_call("claude", Inbox) as session:
        await asyncio.sleep(0.01)
        assert sessions.current is session

    assert session is not idle
    await control.close()


# The Event queue, and blocking.


async def test_wait_for_event_returns_devices_as_they_go_online_and_offline(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    async with mcp(hub, version) as client:
        start = now_ms()
        claimed = await call(client, "wait_for_event", {"timeout_s": 0})
        kitchen = await paired(sim, owner)
        online = await call(client, "wait_for_event")
        await kitchen.close()
        offline = await call(client, "wait_for_event")

    assert claimed == {"events": []}
    assert [without_t(e) for e in online["events"]] == [
        {"event": "device.online", "device": "kitchen"}
    ]
    assert [without_t(e) for e in offline["events"]] == [
        {"event": "device.offline", "device": "kitchen"}
    ]
    assert start <= online["events"][0]["t"] <= offline["events"][0]["t"] <= now_ms()


def without_t(event: JsonObject) -> JsonObject:
    return {key: value for key, value in event.items() if key != "t"}


async def test_gestures_ride_on_the_next_tool_result_errors_included_each_once(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    async with mcp(hub, version) as client:
        kitchen = await online(client, sim, owner)
        await call(client, "set_reflex", {"device": "kitchen", "armed": False})
        await kitchen.wait_for(lambda: not kitchen.armed)
        await kitchen.button.double_click()
        await kitchen.button.long_press()
        # A command the device fails: its reply follows the Events on the device's link.
        kitchen.faults.fail_next()
        failed = await client.call_tool(
            "command", {"address": "kitchen/led", "name": "off", "args": {}}
        )
        again = await client.call_tool("led_release", {"device": "attic"})

    assert failed.is_error
    error = checked(failed)
    assert error["code"] == "failed"
    assert [without_t(e) for e in error["events"]] == [
        {"event": "button.click", "address": "kitchen/button"},
        {"event": "button.click", "address": "kitchen/button"},
        {"event": "button.double_click", "address": "kitchen/button"},
        {"event": "button.long_press", "address": "kitchen/button"},
    ]
    assert "events" not in checked(again)


PHRASE = "Turn the kitchen lights off, please."


async def test_push_to_talk_is_queued_as_its_final_transcript_and_a_click_as_a_gesture(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    async with mcp(hub, version) as client:
        kitchen = await online(client, sim, owner)
        await kitchen.button.click()  # "stop talking": an Utterance with nothing heard
        await kitchen.talk(fake.encode(PHRASE) + sources.silence(300))
        events = await events_until(client, "utterance")

    assert [without_t(e) for e in events] == [
        {"event": "button.click", "address": "kitchen/button"},
        {
            "event": "utterance",
            "address": "kitchen/mic",
            "text": PHRASE,
            "lang": "en",
            "reason": "button",
        },
    ]


async def online(client: Client, sim: Simulator, owner: OwnerClient) -> SimDevice:
    """Claim the hub for `client`, then pair `kitchen`: once the hub has told the Session it's
    online, with nothing else queued."""
    await call(client, "wait_for_event", {"timeout_s": 0})
    kitchen = await paired(sim, owner)
    assert [e["event"] for e in await events_until(client, "device.online")] == ["device.online"]
    return kitchen


async def events_until(client: Client, event: str) -> list[JsonObject]:
    """The Events `wait_for_event` returns, up to the first `event`."""
    events: list[JsonObject] = []
    async with asyncio.timeout(10):
        while not any(e["event"] == event for e in events):
            events += (await call(client, "wait_for_event"))["events"]
    return events


async def test_a_full_queue_drops_its_oldest_event_and_the_next_delivery_says_how_many(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    led_off: JsonObject = {"address": "kitchen/led", "name": "off", "args": {}}
    async with mcp(hub, version) as client:
        kitchen = await online(client, sim, owner)
        await call(client, "set_reflex", {"device": "kitchen", "armed": False})
        await kitchen.wait_for(lambda: not kitchen.armed)
        for _ in range(70):  # 35 double clicks: each is click, click, double_click
            kitchen.button.press()
            kitchen.button.release()
        # Its reply follows the Events on the device's link.
        full = await call(client, "command", led_off)
        kitchen.button.press()
        kitchen.button.release()
        after = await call(client, "command", led_off)

    assert full["dropped"] == 105 - 100
    assert [e["event"] for e in full["events"]] == ["button.double_click"] + [
        "button.click",
        "button.click",
        "button.double_click",
    ] * 33
    assert "dropped" not in after
    assert [e["event"] for e in after["events"]] == ["button.click"]


BLOCK_MS = 300


@pytest.mark.parametrize("hub", [replace(TIMING, mcp_max_block_ms=BLOCK_MS)], indirect=True)
async def test_a_call_that_runs_past_max_block_is_pending_and_its_result_comes_as_an_event(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    async with mcp(hub, version) as client:
        kitchen = await online(client, sim, owner)
        kitchen.mic.feed(says(PHRASE, silence_ms=2 * BLOCK_MS))
        start = time.monotonic()
        pending = await call(client, "listen")
        returned = time.monotonic() - start
        events = await events_until(client, "result")

    assert pending == {"pending": pending["pending"]}
    assert returned < 2 * BLOCK_MS / 1000
    [late] = events
    assert late["pending"] == pending["pending"]
    assert late["tool"] == "listen"
    assert (late["result"]["transcript"], late["result"]["reason"]) == (PHRASE, "end_of_turn")


@pytest.mark.parametrize("hub", [replace(TIMING, mcp_max_block_ms=BLOCK_MS)], indirect=True)
async def test_a_pending_call_that_fails_comes_as_an_event_with_its_error(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    async with mcp(hub, version) as client:
        kitchen = await online(client, sim, owner)
        kitchen.faults.delay_replies_ms = 2 * BLOCK_MS
        kitchen.faults.fail_next()
        pending = await call(
            client, "command", {"address": "kitchen/led", "name": "off", "args": {}}
        )
        [late] = await events_until(client, "result")

    assert late["pending"] == pending["pending"]
    assert late["tool"] == "command"
    assert late["error"]["code"] == "failed"
    assert late["error"]["message"]
    assert "result" not in late


@pytest.mark.parametrize("hub", [replace(TIMING, mcp_progress_ms=100)], indirect=True)
async def test_a_call_sends_progress_while_it_runs_if_the_client_asked_for_it(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    reports: list[float] = []

    async def reported(progress: float, total: float | None, message: str | None) -> None:
        reports.append(progress)

    async with mcp(hub, version) as client:
        kitchen = await online(client, sim, owner)
        kitchen.mic.feed(says(PHRASE, silence_ms=500))
        result = await client.call_tool("listen", {}, progress_callback=reported)

    assert checked(result)["transcript"] == PHRASE
    assert reports
    assert reports == sorted(set(reports))


async def test_a_client_cancel_ends_the_listen(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    async with mcp(hub, version) as client:
        kitchen = await online(client, sim, owner)
        listening = asyncio.create_task(client.call_tool("listen", {}))
        await kitchen.wait_for(lambda: kitchen.status == "listening")
        listening.cancel()
        async with asyncio.timeout(5):
            await kitchen.wait_for(lambda: kitchen.mic.stream is None)
        kitchen.mic.feed(says(PHRASE))
        heard = await call(client, "listen")

    assert heard["transcript"] == PHRASE
    assert "events" not in heard  # nor is the cancelled listen's result


LONG = (
    "One two three four, five six seven eight nine ten eleven twelve. Thirteen fourteen "
    "fifteen, sixteen seventeen eighteen nineteen twenty. Twenty one, twenty two."
)


async def test_a_client_cancel_cuts_the_say(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    async with mcp(hub, version) as client:
        kitchen = await online(client, sim, owner)
        saying = asyncio.create_task(client.call_tool("say", {"text": LONG}))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 10)
        saying.cancel()
        async with asyncio.timeout(5):
            await kitchen.wait_for(lambda: kitchen.speaker.stream is None)
        cut = fake.decode(kitchen.speaker.pcm()).text
        said = await call(client, "say", {"text": "Hello there."})

    assert len(cut) < len(LONG) / 2
    assert said["heard_text"] == "Hello there."
    assert "events" not in said  # nor is the cancelled say's result


async def test_a_say_the_button_cuts_returns_what_the_human_said_while_holding_it(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    async with mcp(hub, version) as client:
        kitchen = await online(client, sim, owner)
        saying = asyncio.create_task(call(client, "say", {"text": LONG}))
        # Past the first clause, 190 ms, well into the second, which ends at 630 ms.
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 35)
        await kitchen.talk(fake.encode(PHRASE) + sources.silence(300))
        said = await saying
        after = await call(client, "wait_for_event", {"timeout_s": 0})

    assert said | {"played_ms": 0} == {
        "reason": "reflex",
        "played_ms": 0,
        "interrupted": True,
        "heard_text": "One two three four,",
        "utterance": {"text": PHRASE, "lang": "en", "reason": "button"},
    }
    assert 190 < said["played_ms"] < 630
    assert after == {"events": []}  # it came once: with the say


async def test_a_say_a_click_cuts_returns_an_utterance_with_nothing_heard(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    async with mcp(hub, version) as client:
        kitchen = await online(client, sim, owner)
        saying = asyncio.create_task(call(client, "say", {"text": LONG}))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 10)
        await kitchen.button.click()
        said = await saying

    assert said["reason"] == "reflex"
    assert said["utterance"] == {"text": "", "reason": "click"}


async def test_a_client_cancel_ends_the_listen_of_a_say_with_then_listen(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    async with mcp(hub, version) as client:
        kitchen = await online(client, sim, owner)
        saying = asyncio.create_task(client.call_tool("say", {"text": "Yes?", "then_listen": True}))
        await kitchen.wait_for(lambda: kitchen.status == "listening")
        saying.cancel()
        async with asyncio.timeout(5):
            await kitchen.wait_for(lambda: kitchen.mic.stream is None)
        after = await call(client, "wait_for_event", {"timeout_s": 0})

    assert after == {"events": []}


async def test_a_say_the_button_cuts_returns_the_utterance_by_a_bare_capability_id_too(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    async with mcp(hub, version) as client:
        kitchen = await online(client, sim, owner)
        saying = asyncio.create_task(call(client, "say", {"text": LONG, "address": "speaker"}))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 10)
        await kitchen.button.click()
        said = await saying

    assert said["utterance"] == {"text": "", "reason": "click"}


async def test_a_client_cancel_of_a_say_the_button_cut_queues_what_the_human_said(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    async with mcp(hub, version) as client:
        kitchen = await online(client, sim, owner)
        saying = asyncio.create_task(client.call_tool("say", {"text": LONG}))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 10)
        kitchen.button.press()
        await kitchen.wait_for(lambda: kitchen.mic.stream is not None)
        kitchen.mic.feed(fake.encode(PHRASE) + sources.silence(300))
        await kitchen.wait_for(lambda: not kitchen.mic.unheard)
        saying.cancel()
        await asyncio.sleep(0.2)  # the cancel reaches the hub
        kitchen.button.release()
        events = await events_until(client, "utterance")

    assert [without_t(e) for e in events if e["event"] == "utterance"] == [
        {
            "event": "utterance",
            "address": "kitchen/mic",
            "text": PHRASE,
            "lang": "en",
            "reason": "button",
        }
    ]


@pytest.mark.parametrize("hub", [replace(TIMING, mcp_idle_ms=IDLE_MS)], indirect=True)
async def test_a_client_cancel_ends_wait_for_event(hub: Hub, version: str) -> None:
    async with mcp(hub, version) as client:
        await call(client, "wait_for_event", {"timeout_s": 0})
        waiting = asyncio.create_task(client.call_tool("wait_for_event", {"timeout_s": 30}))
        await asyncio.sleep(IDLE_MS / 1000)
        waiting.cancel()
        # The Session goes idle, as no call runs.
        async with await claimed(hub):
            pass


async def test_a_press_that_cuts_a_say_while_the_last_press_is_transcribed_is_its_own() -> None:
    reflex = {"address": "kitchen/speaker", "reason": "reflex"}
    inbox = Inbox()
    never = asyncio.Event()
    inbox.notify("say.ended", reflex)
    inbox.notify("utterance.start", pressed(1))
    first = asyncio.ensure_future(inbox.cut("kitchen", never))
    await asyncio.sleep(0)
    inbox.notify("say.ended", reflex)
    second = asyncio.ensure_future(inbox.cut("kitchen", never))
    await asyncio.sleep(0)
    inbox.notify("utterance.start", pressed(2))
    inbox.notify("utterance.end", {"utterance_id": 1, "transcript": "One.", "reason": "button"})
    inbox.notify("utterance.end", {"utterance_id": 2, "transcript": "Two.", "reason": "button"})

    assert await first == {"text": "One.", "reason": "button"}
    assert await second == {"text": "Two.", "reason": "button"}
    assert inbox.take() == {}


def pressed(utterance_id: int) -> JsonObject:
    """The `utterance.start` params of an Utterance a press opened."""
    return {"utterance_id": utterance_id, "address": "kitchen/mic", "trigger": "button"}
