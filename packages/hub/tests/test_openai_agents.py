"""The OpenAI Agents SDK text adapter, `tinyaisense.openai_agents`, running an agent on a
scripted model with no LLM, with the fake speech plugin."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator

import tinyaisense
from agents import Agent, ModelSettings, ModelTracing
from agents.items import TResponseOutputItem, TResponseStreamEvent
from agents.testing import ModelCall, ModelStep, ScriptedModel, assistant_message, function_call
from openai.types.responses import ResponseTextDeltaEvent
from tinyaisense.openai_agents import (
    HubMcpServer,
    InterruptPolicy,
    Turn,
    VoiceAgent,
    interrupt_on_press,
)
from tinyaisense_hub import Hub, OwnerClient
from tinyaisense_hub.speech import fake
from tinyaisense_sim import SimDevice, Simulator, sources

from .test_connection import devices_when, offline
from .test_listen import TOKEN, paired
from .test_reflex import PHRASE, armed, hub
from .test_say import LONG

__all__ = ["hub"]  # the fixture

QUESTION = "What time is it, please?"
"""Longer than a click, as the fake TTS speaks it."""


async def streamed(text: str) -> list[TResponseStreamEvent]:
    """The events a model streams for a reply of `text`, in one text delta."""
    model = ScriptedModel([[assistant_message(text)]])
    events = model.stream_response(
        None,
        "",
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )
    return [event async for event in events]


def reply(
    *deltas: str,
    pause: float = 0.02,
    cancelled: asyncio.Event | None = None,
    fails: Exception | None = None,
) -> ModelStep:
    """A model reply streamed as `deltas`, `pause` s apart. With `cancelled`, it never ends:
    it waits after its deltas until the run is cancelled, then sets `cancelled`. With `fails`,
    it raises it after its deltas."""

    async def stream(call: ModelCall) -> AsyncIterator[TResponseStreamEvent]:
        for event in await streamed("".join(deltas)):
            if not isinstance(event, ResponseTextDeltaEvent):
                yield event
                continue
            for delta in deltas:
                await asyncio.sleep(pause)
                yield event.model_copy(update={"delta": delta})
            if fails is not None:
                raise fails
            if cancelled is not None:
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

    return ModelStep.stream(stream)


async def talk(device: SimDevice, text: str) -> None:
    """Push to talk, saying `text`."""
    await device.talk(fake.encode(text) + sources.silence(100))


class Running:
    """An agent run by voice, in `session`, and the turns it has taken."""

    def __init__(
        self, session: tinyaisense.Session, tools: HubMcpServer | None, turns: asyncio.Queue[Turn]
    ) -> None:
        self.session = session
        self.tools = tools
        """The hub's MCP server that the agent has, if any."""
        self._turns = turns

    async def turn(self) -> Turn:
        """The next turn, once it has ended."""
        async with asyncio.timeout(10):
            return await self._turns.get()


@contextlib.asynccontextmanager
async def by_voice(
    hub: Hub,
    agent: Agent[None],
    policy: InterruptPolicy = interrupt_on_press,
    *,
    tools: bool = False,
) -> AsyncGenerator[Running]:
    """`agent`, run by voice in a Session on `hub` until the block ends; with `tools`, with
    the hub's MCP server joined to it."""
    turns: asyncio.Queue[Turn] = asyncio.Queue()
    async with contextlib.AsyncExitStack() as stack:
        session = await stack.enter_async_context(tinyaisense.connect(hub.agent_url, TOKEN))
        server = None
        if tools:
            server = await stack.enter_async_context(HubMcpServer(session))
            agent = agent.clone(mcp_servers=[server])
        adapter = VoiceAgent(session, agent, policy=policy, on_turn=turns.put_nowait)
        run = asyncio.ensure_future(adapter.run())
        yield Running(session, server, turns)
    await run


async def test_what_the_human_says_is_the_input_and_the_reply_is_spoken_as_it_streams(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    model = ScriptedModel([reply("One two", " three four,", " five six", " seven eight.")])
    async with by_voice(hub, Agent(name="butler", model=model)) as running:
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        turn = await running.turn()

    assert model.first_call is not None
    assert model.first_call.input == [{"role": "user", "content": PHRASE}]
    assert turn.input == PHRASE
    assert not turn.interrupted
    assert turn.spoken == [
        {"reason": "drained", "played_ms": 400, "interrupted": False, "heard_text": LONG}
    ]
    assert fake.decode(kitchen.speaker.pcm()).text == "One two three four,five six seven eight."


async def test_a_press_while_the_agent_speaks_interrupts_its_turn_and_says_what_was_heard(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    cancelled = asyncio.Event()
    model = ScriptedModel(
        [reply("One two three four,", " five six seven eight.", " Nine", cancelled=cancelled)]
    )
    model.enqueue(reply("Okay."))
    async with by_voice(hub, Agent(name="butler", model=model)) as running:
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        # Past the first clause, 190 ms, into the second, which ends at 400 ms.
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 25)
        await talk(kitchen, QUESTION)
        cut, _ = await running.turn(), await running.turn()

    assert cancelled.is_set()
    assert cut.interrupted
    [said] = cut.spoken
    assert said | {"played_ms": 0} == {
        "reason": "reflex",
        "played_ms": 0,
        "interrupted": True,
        "heard_text": "One two three four,",
    }
    assert 190 < said["played_ms"] < 400


async def test_the_next_turn_is_told_what_the_human_heard_of_the_reply_it_cut(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    model = ScriptedModel(
        [reply("One two three four,", " five six seven eight.", " Nine", cancelled=asyncio.Event())]
    )
    model.enqueue(reply("Noon."))
    async with by_voice(hub, Agent(name="butler", model=model)) as running:
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 25)
        await talk(kitchen, QUESTION)
        await running.turn()
        await running.turn()

    assert model.last_call is not None
    assert model.last_call.input == [
        {"role": "user", "content": PHRASE},
        {"role": "assistant", "content": "One two three four,"},
        {
            "role": "user",
            "content": f"you were cut off; the human heard: 'One two three four,'\n\n{QUESTION}",
        },
    ]


async def test_a_reply_cut_once_it_has_streamed_stays_whole_and_the_next_turn_is_told(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    model = ScriptedModel([reply(LONG), reply("Noon.")])
    async with by_voice(hub, Agent(name="butler", model=model)) as running:
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 25)
        await talk(kitchen, QUESTION)
        cut, _ = await running.turn(), await running.turn()

    assert cut.interrupted
    [said] = cut.spoken
    assert said["heard_text"] == "One two three four,"
    assert model.last_call is not None
    assert [item["role"] for item in model.last_call.input] == ["user", "assistant", "user"]
    assert model.last_call.input[1]["content"][0]["text"] == LONG
    assert model.last_call.input[2]["content"] == (
        f"you were cut off; the human heard: 'One two three four,'\n\n{QUESTION}"
    )


async def test_a_press_while_the_agent_is_idle_is_the_next_turn_with_nothing_cut(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    model = ScriptedModel([reply("Hello."), reply("Noon.")])
    async with by_voice(hub, Agent(name="butler", model=model)) as running:
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        first = await running.turn()
        await talk(kitchen, QUESTION)
        second = await running.turn()

    assert not first.interrupted
    assert second.input == QUESTION
    assert not second.interrupted


async def test_a_press_on_a_disarmed_device_never_interrupts(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    model = ScriptedModel([reply("One two three four,", " five six seven eight.", pause=0.2)])
    async with by_voice(hub, Agent(name="butler", model=model)) as running:
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        await running.session.reflex_set("kitchen", armed=False)
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 1)
        await kitchen.button.click()
        turn = await running.turn()

    assert not turn.interrupted
    [said] = turn.spoken
    assert said["reason"] == "drained"
    assert said["heard_text"] == LONG


async def test_a_click_interrupts_and_starts_no_turn_so_the_note_waits_for_the_next(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    model = ScriptedModel(
        [reply("One two three four,", " five six seven eight.", " Nine", cancelled=asyncio.Event())]
    )
    model.enqueue(reply("Noon."))
    async with by_voice(hub, Agent(name="butler", model=model)) as running:
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 25)
        await kitchen.button.click()
        cut = await running.turn()
        await talk(kitchen, QUESTION)
        after = await running.turn()

    assert cut.interrupted
    assert len(model.calls) == 2
    assert after.input == f"you were cut off; the human heard: 'One two three four,'\n\n{QUESTION}"


def any_press(event: tinyaisense.Notification, armed: bool) -> bool:
    return event.method == "button.down"


async def test_a_policy_that_interrupts_where_the_reflex_cut_nothing_cuts_the_speech(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    cancelled = asyncio.Event()
    model = ScriptedModel(
        [reply("One two three four,", " five six seven eight.", " Nine", cancelled=cancelled)]
    )
    async with by_voice(hub, Agent(name="butler", model=model), policy=any_press) as running:
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        await running.session.reflex_set("kitchen", armed=False)
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 25)
        await kitchen.button.click()
        cut = await running.turn()

    assert cancelled.is_set()
    assert cut.interrupted
    [said] = cut.spoken
    assert said["reason"] == "preempted"
    assert said["heard_text"] == "One two three four,"


async def test_the_agent_calls_the_hubs_tools_in_its_session_and_is_told_how_to_use_them(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    model = ScriptedModel([[function_call("say", {"text": "Hello."}, call_id="1")], reply("Bye.")])
    agent = Agent(name="butler", instructions="Be brief.", model=model)
    async with by_voice(hub, agent, tools=True) as running:
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        await running.turn()
        assert running.tools is not None
        guide = running.tools.instructions

    assert running.tools.client_session_timeout_seconds == 60
    assert guide is not None
    assert guide.startswith("tinyaisense lets you hear and speak to a human")
    assert model.first_call is not None
    assert model.first_call.system_instructions == f"Be brief.\n\n{guide}"
    tools = [tool.name for tool in model.first_call.tools]
    assert "say" in tools
    assert "wait_for_event" not in tools  # a link joined to an agent-API Session has no queue
    assert fake.decode(kitchen.speaker.pcm()).text == "Hello.Bye."


async def test_a_device_the_agent_disarms_through_the_hubs_tools_never_interrupts(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    disarm = function_call("set_reflex", {"device": "kitchen", "armed": False}, call_id="1")
    model = ScriptedModel(
        [[disarm], reply("One two three four,", " five six seven eight.", pause=0.2)]
    )
    async with by_voice(hub, Agent(name="butler", model=model), tools=True) as running:
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 1)
        await kitchen.button.click()
        turn = await running.turn()

    assert not kitchen.armed
    assert not turn.interrupted
    [said] = turn.spoken
    assert said["heard_text"] == LONG


async def test_a_turn_that_fails_says_what_it_had_and_the_next_one_runs(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    down = RuntimeError("the model is down")
    model = ScriptedModel([reply("One two three four,", " five six", fails=down), reply("Noon.")])
    async with by_voice(hub, Agent(name="butler", model=model)) as running:
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        failed = await running.turn()
        await talk(kitchen, QUESTION)
        after = await running.turn()

    assert failed.error is down
    assert [said["heard_text"] for said in failed.spoken] == ["One two three four, five six"]
    assert after.error is None
    assert [said["heard_text"] for said in after.spoken] == ["Noon."]


async def test_a_reply_to_a_device_that_went_offline_is_not_spoken_and_the_agent_goes_on(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    gone = asyncio.Event()

    async def once_gone(call: ModelCall) -> list[TResponseOutputItem]:
        await gone.wait()
        return [assistant_message("Hello.")]

    model = ScriptedModel([ModelStep.respond(once_gone), reply("Noon.")])
    async with by_voice(hub, Agent(name="butler", model=model)) as running:
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        await kitchen.close()
        async with asyncio.timeout(5):
            await devices_when(owner, offline)
        gone.set()
        unspoken = await running.turn()
        hall = await paired(sim, owner, "hall")
        await armed(hall)
        await talk(hall, QUESTION)
        after = await running.turn()

    assert unspoken.spoken == []
    assert unspoken.error is None
    assert [said["heard_text"] for said in after.spoken] == ["Noon."]


async def test_the_agent_runs_until_the_session_ends_mid_turn_and_its_run_stops(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    cancelled = asyncio.Event()
    model = ScriptedModel([reply("One two three four,", " five", cancelled=cancelled)])
    async with by_voice(hub, Agent(name="butler", model=model)):
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 1)

    async with asyncio.timeout(5):
        await cancelled.wait()


async def test_text_before_a_tool_call_is_spoken_before_the_say_the_tool_makes_and_holds_it_up_not(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    speaks = [assistant_message("Hello."), function_call("say", {"text": "Bye."}, call_id="1")]
    model = ScriptedModel([speaks, reply("Done.")])
    async with by_voice(hub, Agent(name="butler", model=model), tools=True) as running:
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        turn = await running.turn()

    assert fake.decode(kitchen.speaker.pcm()).text == "Hello.Bye.Done."
    assert [said["heard_text"] for said in turn.spoken] == ["Hello.", "Done."]


async def test_the_next_turn_is_told_what_the_human_heard_of_a_say_the_agent_called(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    speaks = function_call("say", {"text": LONG + " Nine ten."}, call_id="1")
    model = ScriptedModel([[speaks], reply("Noon.")])
    async with by_voice(hub, Agent(name="butler", model=model), tools=True) as running:
        await armed(kitchen)
        await talk(kitchen, PHRASE)
        await kitchen.wait_for(lambda: len(kitchen.speaker.captured) >= 25)
        await talk(kitchen, QUESTION)
        cut, after = await running.turn(), await running.turn()

    assert cut.interrupted
    assert cut.spoken == []
    assert after.input == f"you were cut off; the human heard: 'One two three four,'\n\n{QUESTION}"
