"""The OpenAI Agents SDK text adapter: an Agents SDK agent, run by voice.

What the human says by push-to-talk is the input of a turn of the agent, run with
`Runner.run_streamed`, and its reply is spoken as it streams, with `say.open/append/close` on
the speaker of the device they spoke to: one `say` for each model response. An Event, by
default a button press, interrupts the running turn: its run is cancelled, and its speech cut.

`HubMcpServer` gives the agent the hub's MCP tools, in the same Session.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

from agents import Agent, RunContextWrapper, Runner, SQLiteSession
from agents.mcp import MCPServerStreamableHttp
from agents.memory import Session as History
from agents.result import RunResultStreaming
from agents.stream_events import RawResponsesStreamEvent
from mcp.types import CallToolResult
from openai.types.responses import ResponseCompletedEvent, ResponseTextDeltaEvent
from tinyaisense_wire import X_HUB_SESSION, JsonObject

from .session import ApiError, Notification, Session, SessionEnded

_log = logging.getLogger("tinyaisense.openai_agents")

CUT_OFF = "you were cut off; the human heard: '{heard}'"
"""The note that the next user message starts with, once the human has cut off a reply."""

InterruptPolicy = Callable[[Notification, bool], bool]
"""Whether a device Event interrupts the running turn, given the Event and whether its
device's Reflex is armed."""


def interrupt_on_press(event: Notification, armed: bool) -> bool:
    """The default Interrupt policy: `button.down` on a device whose Reflex is armed, and has
    so cut its speaker. A click starts with one, so it interrupts too, and its Utterance
    starts no turn. A disarmed device never interrupts."""
    return armed and event.method == "button.down"


@dataclass(frozen=True)
class Turn:
    """A turn the agent took, once it has ended."""

    input: str
    """The user message the agent got."""
    spoken: list[JsonObject]
    """How its reply was spoken: the `say` result, `{reason, played_ms, interrupted,
    heard_text}`, of each model response it spoke text in."""
    interrupted: bool
    """Whether it was interrupted."""
    result: RunResultStreaming
    """The Agents SDK's run of it."""
    error: Exception | None = None
    """What its run failed with, if it failed. What it had streamed is spoken."""


class HubMcpServer(MCPServerStreamableHttp):
    """The hub's MCP server, joined to the Session `hub`: the hub's tools, for the agent to
    call in it. Connect it before the agent runs, e.g. `async with HubMcpServer(hub):`.

    A `VoiceAgent` adds its instructions, how to use the tools, to its agent's. Calls such as
    `say` block for up to 50 s, so the MCP read timeout is 60 s.
    """

    def __init__(
        self,
        hub: Session,
        *,
        name: str = "tinyaisense",
        client_session_timeout_seconds: float = 60,
        cache_tools_list: bool = True,
    ) -> None:
        url = urlsplit(hub.url)
        scheme = "https" if url.scheme == "wss" else "http"
        path = url.path.rpartition("/")[0] + "/mcp"
        super().__init__(
            {
                "url": url._replace(scheme=scheme, path=path).geturl(),
                "headers": {"Authorization": f"Bearer {hub.token}", X_HUB_SESSION: hub.id},
            },
            cache_tools_list=cache_tools_list,
            name=name,
            client_session_timeout_seconds=client_session_timeout_seconds,
        )
        self._hub = hub

    @property
    def instructions(self) -> str | None:
        """The server's instructions, once it's connected."""
        return self.session.instructions if self.session is not None else None

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None, meta: dict[str, Any] | None = None
    ) -> CallToolResult:
        result = await super().call_tool(tool_name, arguments, meta)
        if tool_name == "set_reflex" and arguments is not None and not result.is_error:
            # The Session keeps what the agent's Reflex is, as its `reflex_set` does.
            if arguments["armed"]:
                self._hub.disarmed.discard(arguments["device"])
            else:
                self._hub.disarmed.add(arguments["device"])
        return result


class VoiceAgent:
    """Runs `agent` by voice, in the hub Session `hub`, one turn at a time.

    What the human says by push-to-talk is the input of each turn: the Session must opt in
    to transcripts, as it does by default. The conversation lives in `session`, by default
    in memory.
    """

    def __init__(
        self,
        hub: Session,
        agent: Agent[Any],
        *,
        session: History | None = None,
        policy: InterruptPolicy = interrupt_on_press,
        on_turn: Callable[[Turn], object] | None = None,
    ) -> None:
        self._hub = hub
        self._agent = agent
        self._history = session if session is not None else SQLiteSession(hub.id)
        self._policy = policy
        self._on_turn = on_turn
        self._turn: _Turn | None = None
        """The turn running, if any."""
        self._last: _Turn | None = None
        """The turn running, or else the last one."""
        self._manifests: dict[str, JsonObject] = {}
        """Each device's Manifest, by name."""
        self._pressed: dict[int, str] = {}
        """The device of each push-to-talk Utterance under way, by id."""
        self._utterances: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        """What the human said, and to which device, for the turns to come."""

    async def run(self) -> None:
        """Run the agent until the Session ends. Raises what the policy or `on_turn` raise."""
        for device in await self._hub.devices():
            self._manifests[device["name"]] = device["manifest"]
        tasks = (asyncio.ensure_future(self._follow()), asyncio.ensure_future(self._take_turns()))
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            if not task.cancelled() and (error := task.exception()) is not None:
                raise error

    async def _follow(self) -> None:
        """Follow the Session's notifications: interrupt the running turn, and queue what the
        human says for the turns to come."""
        async for notification in self._hub.notifications():
            method, params = notification.method, notification.params
            if "data" in params:  # a device Event
                device = _device(params["address"])
                armed = device not in self._hub.disarmed
                if self._turn is not None and self._policy(notification, armed):
                    # Unless the Reflex has cut the turn's speech, the adapter cuts it.
                    reflex = armed and method == "button.down" and device == self._turn.device
                    self._turn.interrupt(cut=not reflex)
            elif method == "say.ended":
                if self._last is not None and params["address"] == self._last.speaker:
                    self._last.heard.append(params)
            elif method == "device.online":
                self._manifests[params["device"]] = params["manifest"]
            elif method == "utterance.start" and params["trigger"] == "button":
                self._pressed[params["utterance_id"]] = _device(params["address"])
            elif method == "utterance.end":
                device = self._pressed.pop(params["utterance_id"], None)
                transcript: str = params.get("transcript", "")
                if device is not None and params["reason"] != "click" and transcript:
                    self._utterances.put_nowait((device, transcript))

    async def _take_turns(self) -> None:
        """Take a turn for each Utterance, one at a time."""
        while True:
            device, text = await self._utterances.get()
            turn = await self._take(device, text)
            if self._on_turn is not None:
                self._on_turn(turn)

    async def _take(self, device: str, text: str) -> Turn:
        """Take a turn on what the human said, `text`, to `device`."""
        if (note := self._cut_off()) is not None:
            text = f"{note}\n\n{text}"
        result = Runner.run_streamed(self._instructed(), text, session=self._history)
        turn = _Turn(self._hub, device, self._speaker(device), result)
        self._turn = self._last = turn
        try:
            spoken = await turn.take()
        finally:
            self._turn = None
        for step, said in spoken:
            heard: str = said["heard_text"]
            if step == turn.dropped and heard:
                # The cancelled run kept none of this reply: keep what the human heard of it.
                await self._history.add_items([{"role": "assistant", "content": heard}])
        return Turn(text, [said for _, said in spoken], turn.interrupted, result, turn.error)

    def _cut_off(self) -> str | None:
        """The note for the next turn, `CUT_OFF`, if the human cut off the last one: what they
        heard of it, from its own `say`s and those of its tools. Each one's `say.ended` comes
        before the Utterance that the next turn takes."""
        last = self._last
        if last is None:
            return None
        if not last.interrupted and not any(said["interrupted"] for said in last.heard):
            return None
        heard = " ".join(said["heard_text"] for said in last.heard if said["heard_text"])
        return CUT_OFF.format(heard=heard)

    def _instructed(self) -> Agent[Any]:
        """The agent, with the instructions of the hub's MCP servers it has after its own."""
        agent = self._agent
        guides = [
            server.instructions
            for server in agent.mcp_servers
            if isinstance(server, HubMcpServer) and server.instructions
        ]
        if not guides:
            return agent

        async def instructions(context: RunContextWrapper[Any], _: Agent[Any]) -> str:
            own = await agent.get_system_prompt(context)
            return "\n\n".join(text for text in (own, *guides) if text)

        return agent.clone(instructions=instructions)

    def _speaker(self, device: str) -> str | None:
        """The Address of `device`'s speaker, if it has one."""
        manifest = self._manifests.get(device, {})
        for capability in cast(list[JsonObject], manifest.get("capabilities", [])):
            if capability["kind"] == "speaker":
                return f"{device}/{capability['id']}"
        return None


class _Turn:
    """A turn running on `device`, speaking on `speaker`: its run, and its speech."""

    def __init__(
        self, hub: Session, device: str, speaker: str | None, result: RunResultStreaming
    ) -> None:
        self.device = device
        self.speaker = speaker
        self.result = result
        self.interrupted = False
        self.dropped: int | None = None
        """The step, the model call and its tools, that cancelling its run dropped, if it was
        cancelled before it ended."""
        self.heard: list[JsonObject] = []
        """Each `say.ended` on its speaker, from its start until the next turn's."""
        self.error: Exception | None = None
        self._speech = _Speech(hub, speaker)
        self._cut: asyncio.Future[None] | None = None

    async def take(self) -> list[tuple[int, JsonObject]]:
        """Take the turn, speaking its reply as it streams. Returns how each model response
        was spoken, by step."""
        try:
            async for event in self.result.stream_events():
                if self.interrupted or not isinstance(event, RawResponsesStreamEvent):
                    continue
                if isinstance(event.data, ResponseTextDeltaEvent):
                    await self._speech.append(self.result.current_turn, event.data.delta)
                elif isinstance(event.data, ResponseCompletedEvent):
                    self._speech.close()  # so that the `say`s its tools make follow it
        except Exception as e:  # the next Utterance gets a turn all the same
            _log.exception("the agent's turn failed")
            self.error = e
        spoken = await self._speech.spoken()
        if self._cut is not None:
            await self._cut
        return spoken

    def interrupt(self, *, cut: bool) -> None:
        """Interrupt the turn: cancel its run, if it's running, and with `cut`, its speech."""
        self.interrupted = True
        if self.dropped is None and not self.result.is_complete:
            self.dropped = self.result.current_turn
            self.result.cancel("immediate")
        if cut and self._cut is None:
            self._cut = asyncio.ensure_future(self._speech.cut())


class _Speech:
    """A turn's reply, spoken on `speaker` as its text streams: one `say` for each model
    response. If one fails, e.g. the device went offline, the rest of the reply goes
    unspoken."""

    def __init__(self, hub: Session, speaker: str | None) -> None:
        self._hub = hub
        self._speaker = speaker
        self._handle: int | None = None
        """The `say` open, if any."""
        self._step = 0
        """The step of the `say` open."""
        self._closing: list[tuple[int, asyncio.Future[JsonObject | None]]] = []
        """Each `say` closed, by step: its result, once it has ended."""
        self._cut = False
        self._failed = False

    async def append(self, step: int, text: str) -> None:
        """Speak `text`, of the model response of `step`, in its `say`."""
        if self._speaker is None or self._cut or self._failed:
            return
        try:
            if self._handle is None:
                self._handle, self._step = await self._hub.say_open(self._speaker), step
                if self._cut:
                    return  # it was cut before it opened: it stays empty
            await self._hub.say_append(self._handle, text)
        except (ApiError, SessionEnded) as e:
            self._fail(e)

    def close(self) -> None:
        """Close the `say` open, if any, without waiting for it to end."""
        if self._handle is not None and not self._failed:
            closing = asyncio.ensure_future(self._close(self._handle))
            self._closing.append((self._step, closing))
        self._handle = None

    async def _close(self, handle: int) -> JsonObject | None:
        try:
            return await self._hub.say_close(handle)
        except (ApiError, SessionEnded) as e:
            self._fail(e)
            return None

    async def spoken(self) -> list[tuple[int, JsonObject]]:
        """Close the `say` open, if any, and wait for every `say` to end. Returns their
        results, by step."""
        self.close()
        spoken = [(step, await closing) for step, closing in self._closing]
        return [(step, said) for step, said in spoken if said is not None]

    def _fail(self, error: ApiError | SessionEnded) -> None:
        _log.warning("can't speak the reply on %s: %s", self._speaker, error)
        self._failed = True

    async def cut(self) -> None:
        """Cut the speech off, and any `say` its tools make, with a `say` of nothing that
        replaces them."""
        self._cut = True
        if self._speaker is not None:
            with contextlib.suppress(ApiError, SessionEnded):
                await self._hub.say(self._speaker, "", replace=True)


def _device(address: str) -> str:
    return address.partition("/")[0]


__all__ = ["CUT_OFF", "HubMcpServer", "InterruptPolicy", "Turn", "VoiceAgent", "interrupt_on_press"]
