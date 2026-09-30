"""The `tinyaisense mcp` stdio shim, driven by the MCP SDK's stdio client as Claude Code
drives it: over the initialize handshake."""

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import AsyncGenerator, Sequence
from dataclasses import replace

import pytest
import tinyaisense
from mcp import Client, StdioServerParameters
from mcp.client.extension import ClientExtension, NotificationBinding
from mcp.types import NotificationParams
from tinyaisense_hub import Hub, OwnerClient
from tinyaisense_hub.speech import fake
from tinyaisense_sim import Simulator, sources

from .conftest import StartHub
from .test_listen import TIMING, paired, says
from .test_mcp import BLOCK_MS, LONG, PHRASE, TOKEN, call, hub
from .test_reflex import armed

__all__ = ["hub"]  # the fixture


def hub_url(hub: Hub) -> str:
    return hub.mcp_url.removesuffix("/mcp")


def shim_command(hub: Hub, *args: str, token: str = TOKEN) -> StdioServerParameters:
    """`tinyaisense mcp` for `hub`, with the agent token in its env."""
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "tinyaisense", "mcp", "--hub", hub_url(hub), *args],
        env={**os.environ, "TINYAISENSE_TOKEN": token},
    )


class Pushed(NotificationParams):
    """A Claude Code channel event."""

    content: str
    meta: dict[str, str] | None = None  # pyright: ignore[reportIncompatibleVariableOverride]


class Channel(ClientExtension):
    """What Claude Code gets of the channel events a server pushes."""

    identifier = "com.anthropic/claude-channel"

    def __init__(self) -> None:
        self._pushed: asyncio.Queue[Pushed] = asyncio.Queue()

    def notifications(self) -> Sequence[NotificationBinding[Pushed]]:
        return [
            NotificationBinding(
                method="notifications/claude/channel", params_type=Pushed, handler=self._push
            )
        ]

    async def _push(self, params: Pushed) -> None:
        self._pushed.put_nowait(params)

    async def next(self) -> tuple[str, dict[str, str]]:
        """The next channel event: its content and meta."""
        async with asyncio.timeout(10):
            pushed = await self._pushed.get()
        return pushed.content, pushed.meta or {}


@contextlib.asynccontextmanager
async def shim(hub: Hub, *args: str, channel: Channel | None = None) -> AsyncGenerator[Client]:
    """An MCP client of the shim, run with `args`; its channel events go to `channel`."""
    extensions = [channel] if channel is not None else []
    async with Client(shim_command(hub, *args), mode="legacy", extensions=extensions) as client:
        yield client


async def test_the_shim_serves_the_hubs_tools_in_the_session_it_holds(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    async with shim(hub) as client:
        tools = (await client.list_tools()).tools
        said = await call(client, "say", {"text": "Hello there."})
        with pytest.raises(tinyaisense.ApiError) as refused:
            await tinyaisense.connect(hub.agent_url, TOKEN)

    assert "say" in [tool.name for tool in tools]
    assert "wait_for_event" not in [tool.name for tool in tools]
    assert said["reason"] == "drained"
    assert kitchen.speaker.pcm()
    assert refused.value.code == "busy"
    assert refused.value.data["holder"] == "agent_api"


async def started(command: Hub | StdioServerParameters) -> asyncio.subprocess.Process:
    """The shim, run as `command`, or with no args for a `Hub`, with its stdin kept open, as
    MCP clients keep it."""
    if isinstance(command, Hub):
        command = shim_command(command)
    return await asyncio.create_subprocess_exec(
        command.command,
        *command.args,
        env=command.env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def initialized(shim: asyncio.subprocess.Process) -> None:
    """Wait until the shim has answered `initialize`: it serves MCP, in its Session."""
    assert shim.stdin is not None
    assert shim.stdout is not None
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "raw", "version": "1"},
        },
    }
    shim.stdin.write(json.dumps(initialize).encode() + b"\n")
    async with asyncio.timeout(10):
        reply = json.loads(await shim.stdout.readline())
    assert reply["id"] == 1
    assert "result" in reply


async def exited(shim: asyncio.subprocess.Process) -> tuple[int, str]:
    """Wait until the shim exits by itself: its exit code, and what it wrote to stderr."""
    assert shim.stderr is not None
    async with asyncio.timeout(10):
        stderr = await shim.stderr.read()
        code = await shim.wait()
    return code, stderr.decode()


async def test_a_busy_hub_makes_the_shim_exit_with_a_message_that_says_who_holds_it(
    hub: Hub,
) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN):
        code, stderr = await exited(await started(hub))

    assert code == 1
    [message] = stderr.splitlines()
    assert message.startswith("tinyaisense mcp: another agent holds the hub (agent_api, since ")
    assert message.endswith("); --takeover takes it over")


async def test_a_hub_the_shim_cant_reach_makes_it_exit_with_a_message(hub: Hub) -> None:
    url, shim = hub_url(hub), shim_command(hub)
    await hub.close()
    code, stderr = await exited(await started(shim))

    assert code == 1
    [message] = stderr.splitlines()
    assert message.startswith(f"tinyaisense mcp: can't reach the hub at {url}: ")


async def test_a_hub_whose_mcp_server_refuses_the_shim_makes_it_exit_with_a_message(
    start_hub: StartHub,
) -> None:
    hub = await start_hub(allowed_hosts=("hub.local:*",))  # not the address the shim uses
    code, stderr = await exited(await started(hub))

    assert code == 1
    [message] = stderr.splitlines()
    assert message.startswith("tinyaisense mcp: the hub's MCP server refused the shim: ")


async def test_with_takeover_the_shim_takes_the_hub_from_the_agent_that_holds_it(
    hub: Hub,
) -> None:
    async with tinyaisense.connect(hub.agent_url, TOKEN) as holder:
        async with shim(hub, "--takeover") as client:
            await call(client, "voices")
        ended = [n async for n in holder.notifications()]

    assert [(n.method, n.params["reason"]) for n in ended] == [("session.ended", "taken_over")]


async def test_the_shim_exits_once_its_session_ends(hub: Hub) -> None:
    shim = await started(hub)
    await initialized(shim)
    async with tinyaisense.connect(hub.agent_url, TOKEN, takeover=True):
        code, stderr = await exited(shim)

    assert code == 1
    assert stderr.splitlines() == ["tinyaisense mcp: the Session ended: taken_over"]


async def test_with_channel_the_shim_pushes_push_to_talk_and_gestures_into_claude_code(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    channel = Channel()
    async with shim(hub, "--channel", channel=channel) as client:
        await armed(kitchen)
        await kitchen.button.click()  # a gesture, and an Utterance with nothing heard
        await kitchen.talk(fake.encode(PHRASE) + sources.silence(300))
        events = [await channel.next(), await channel.next()]
        capabilities = client.server_capabilities
        instructions = client.instructions

    assert capabilities.experimental == {"claude/channel": {}}
    assert instructions is not None
    assert "<channel" in instructions
    assert events == [
        ("button.click", {"event": "button.click", "address": "kitchen/button"}),
        (
            PHRASE,
            {"event": "utterance", "address": "kitchen/mic", "lang": "en", "reason": "button"},
        ),
    ]


async def test_with_channel_what_comes_before_claude_code_has_initialized_comes_once_it_has(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    shim = await started(shim_command(hub, "--channel"))
    assert shim.stdin is not None
    assert shim.stdout is not None
    await armed(kitchen)  # by the shim's Session
    await kitchen.button.click()
    await asyncio.sleep(0.5)  # for the shim to hear it
    await initialized(shim)
    shim.stdin.write(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}\n')
    async with asyncio.timeout(10):
        pushed = json.loads(await shim.stdout.readline())
    shim.stdin.close()
    await exited(shim)

    assert pushed["method"] == "notifications/claude/channel"
    assert pushed["params"] == {
        "content": "button.click",
        "meta": {"event": "button.click", "address": "kitchen/button"},
    }


@pytest.mark.parametrize("hub", [replace(TIMING, mcp_max_block_ms=BLOCK_MS)], indirect=True)
async def test_with_channel_the_result_of_a_call_that_returned_pending_comes_as_a_channel_event(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(says(PHRASE, silence_ms=2 * BLOCK_MS))
    channel = Channel()
    async with shim(hub, "--channel", channel=channel) as client:
        pending = await call(client, "listen")
        content, meta = await channel.next()

    assert meta == {"event": "result", "pending": str(pending["pending"]), "tool": "listen"}
    assert json.loads(content)["result"]["transcript"] == PHRASE


async def test_with_channel_what_cut_a_say_comes_in_its_result_not_as_a_channel_event_too(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    channel = Channel()
    async with shim(hub, "--channel", channel=channel) as client:
        await armed(kitchen)
        saying = asyncio.create_task(call(client, "say", {"text": LONG}))
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 10)
        await kitchen.talk(fake.encode(PHRASE) + sources.silence(300))
        said = await saying
        await kitchen.button.click()
        pushed = await channel.next()

    assert said["utterance"] == {"text": PHRASE, "lang": "en", "reason": "button"}
    assert pushed == ("button.click", {"event": "button.click", "address": "kitchen/button"})
