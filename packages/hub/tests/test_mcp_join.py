"""MCP links that join an agent-API Session with `X-Hub-Session`."""

import asyncio
from dataclasses import replace

import pytest
import tinyaisense
from mcp.shared.exceptions import MCPError
from tinyaisense_hub import Hub, OwnerClient
from tinyaisense_hub.speech import fake
from tinyaisense_sim import Simulator, sources

from .test_listen import TIMING, paired, says, utterance
from .test_mcp import BLOCK_MS, LONG, PHRASE, TOKEN, ToolError, call, hub, mcp, now_ms, version
from .test_reflex import armed

__all__ = ["hub", "version"]  # the fixtures


async def notified(agent: tinyaisense.Session, method: str) -> tinyaisense.Notification:
    """The agent link's next `method` notification, past others."""
    async with asyncio.timeout(10):
        async for notification in agent.notifications():
            if notification.method == method:
                return notification
    raise AssertionError("the Session ended")


async def test_an_mcp_link_with_x_hub_session_acts_in_that_agent_api_session(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    await paired(sim, owner)
    async with (
        tinyaisense.connect(hub.agent_url, TOKEN) as agent,
        mcp(hub, version, session=agent.id) as client,
    ):
        said = await call(client, "say", {"text": "Hello there."})
        ended = await notified(agent, "say.ended")

    assert said["reason"] == "drained"
    assert ended.params["text"] == "Hello there."


async def test_a_stale_x_hub_session_gets_disconnected_for_every_tool(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        pass
    async with mcp(hub, version, session=agent.id) as client:
        with pytest.raises(ToolError) as said:
            await call(client, "say", {"text": "Hello there."})
        with pytest.raises(ToolError) as listed:
            await call(client, "devices")
        async with tinyaisense.connect(hub.agent_url, TOKEN):
            pass  # nor did it claim the hub

    assert said.value.code == "disconnected"
    assert listed.value.code == "disconnected"


async def test_a_joined_link_has_no_event_queue_as_the_agent_api_link_gets_every_event(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    kitchen = await paired(sim, owner)
    async with (
        tinyaisense.connect(hub.agent_url, TOKEN) as agent,
        mcp(hub, version, session=agent.id) as client,
    ):
        tools = (await client.list_tools()).tools
        with pytest.raises(MCPError):
            await client.call_tool("wait_for_event", {"timeout_s": 0})
        await call(client, "set_reflex", {"device": "kitchen", "armed": False})
        await kitchen.wait_for(lambda: not kitchen.armed)
        await kitchen.button.click()
        clicked = await notified(agent, "button.click")
        # A command's reply follows the Events on the device's link.
        after = await call(client, "command", {"address": "kitchen/led", "name": "off", "args": {}})

    assert "wait_for_event" not in [tool.name for tool in tools]
    assert "say" in [tool.name for tool in tools]
    assert clicked.params["address"] == "kitchen/button"
    assert after == {}


async def test_a_joined_say_returns_what_the_button_cut_it_with_and_the_agent_link_hears_it_too(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    kitchen = await paired(sim, owner)
    async with (
        tinyaisense.connect(hub.agent_url, TOKEN) as agent,
        mcp(hub, version, session=agent.id) as client,
    ):
        await armed(kitchen)
        saying = asyncio.create_task(call(client, "say", {"text": LONG}))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 10)
        await kitchen.talk(fake.encode(PHRASE) + sources.silence(300))
        said = await saying
        heard = await utterance(agent)

    assert said["interrupted"] is True
    assert said["utterance"] == {"text": PHRASE, "lang": "en", "reason": "button"}
    assert heard[-1].params["transcript"] == PHRASE


async def test_a_joined_listen_hears_a_transcript_whatever_the_sessions_utterance_opt_in(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says("Hello there, how are you?"))
    opt_in = {"transcript": False, "audio": True}
    async with (
        tinyaisense.connect(hub.agent_url, TOKEN, utterances=opt_in) as agent,
        mcp(hub, version, session=agent.id) as client,
    ):
        result = await call(client, "listen")
        heard = await utterance(agent)

    assert result["transcript"] == "Hello there, how are you?"
    assert "handle" not in heard[0].params  # no audio


@pytest.mark.parametrize("hub", [replace(TIMING, mcp_max_block_ms=BLOCK_MS)], indirect=True)
async def test_a_joined_call_past_max_block_is_pending_and_its_result_goes_to_the_agent_api_link(
    hub: Hub, sim: Simulator, owner: OwnerClient, version: str
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says(PHRASE, silence_ms=2 * BLOCK_MS))
    async with (
        tinyaisense.connect(hub.agent_url, TOKEN) as agent,
        mcp(hub, version, session=agent.id) as client,
    ):
        start = now_ms()
        pending = await call(client, "listen")
        late = await notified(agent, "mcp.result")

    assert pending == {"pending": pending["pending"]}
    assert late.params["pending"] == pending["pending"]
    assert late.params["tool"] == "listen"
    assert late.params["result"]["transcript"] == PHRASE
    assert start + 2 * BLOCK_MS <= late.t <= now_ms()


async def test_a_joined_link_gets_instructions_without_the_event_queue_or_the_claim(
    hub: Hub, version: str
) -> None:
    async with (
        tinyaisense.connect(hub.agent_url, TOKEN) as agent,
        mcp(hub, version, session=agent.id) as client,
    ):
        instructions = client.instructions

    assert instructions is not None
    assert len(instructions) < 2048
    first, _, rest = instructions.partition("\n\n")
    assert len(first) <= 512
    assert rest
    for topic in ("listen", "say", "half-duplex", "interrupted", "pending"):
        assert topic in instructions
    for topic in ("wait_for_event", "`events`", "claims"):
        assert topic not in instructions
