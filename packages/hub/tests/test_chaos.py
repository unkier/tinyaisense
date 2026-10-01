"""A seeded chaos soak: devices under UDP loss, bursts and jitter, knocked off again and again,
while an agent keeps working them. Afterwards no Session is stuck, the stream counters add up
and no task is left behind.

`TINYAISENSE_SOAK_S` runs it longer, and `TINYAISENSE_SOAK_SEED` with other faults.
"""

import asyncio
import contextlib
import os
import random
from collections.abc import Awaitable, Callable

import pytest
import tinyaisense
from tinyaisense_hub import Hub, OwnerClient
from tinyaisense_sim import GilbertElliott, Light, SimDevice, Simulator, sources
from tinyaisense_sim.pytest_plugin import SimulatorFactory
from tinyaisense_wire import HubAudio

from .conftest import StartHub
from .test_faults import TOKEN, next_of
from .test_say import SPEECH

SOAK_S = float(os.environ.get("TINYAISENSE_SOAK_S", "8"))
SEED = int(os.environ.get("TINYAISENSE_SOAK_SEED", "1"))
DEVICES = {"kitchen": "m5stack-atom-echo", "hall": "m5stack-atom-echo", "den": "sim-duplex"}
BYTES_PER_MS = 32
"""Of 16 kHz s16le mono."""
STUCK_S = 5
"""An agent call that takes longer than this, faults and all, is stuck."""


@pytest.fixture
async def hub(start_hub: StartHub) -> Hub:
    return await start_hub(speech=SPEECH)


@pytest.fixture
def sim(hub: Hub, simulator: SimulatorFactory) -> Simulator:
    return simulator(hub.device_address, seed=SEED)


def lossy(device: SimDevice) -> None:
    """5% loss each way, bursts of it, jitter and the odd duplicate."""
    for way in (device.faults.udp_down, device.faults.udp_up):
        way.loss = 0.05
        way.bursts = GilbertElliott(p=0.01, r=0.3)
        way.jitter_ms = 15
        way.duplicate = 0.01


def calm(device: SimDevice) -> None:
    for way in (device.faults.udp_down, device.faults.udp_up):
        way.loss = way.duplicate = way.jitter_ms = 0
        way.bursts = None
    device.faults.wifi_down = False


async def chaos(devices: list[SimDevice], rng: random.Random, until: float) -> None:
    """Knock a device off, or work its button, every few hundred ms."""
    loop = asyncio.get_running_loop()
    clicks: list[asyncio.Task[None]] = []
    while loop.time() < until:
        await asyncio.sleep(rng.uniform(0.2, 0.6))
        device = rng.choice(devices)
        faults, button = device.faults, device.button
        match rng.randrange(9):
            case 0:
                faults.rst()
            case 1:
                faults.close()
            case 2:
                faults.vanish()
            case 3:
                faults.wifi_drop(rng.uniform(100, 1200))
            case 4:
                faults.reboot(rng.uniform(100, 500), "panic")
            case 5:
                faults.send_garbage()
            case 6:
                clicks.append(asyncio.create_task(button.click()))
            case 7:
                clicks.append(asyncio.create_task(button.double_click()))
            case _:
                clicks.append(asyncio.create_task(button.click(rng.uniform(200, 800))))
    await asyncio.gather(*clicks)


class Workload:
    """Works one device's speaker, mic and LED over and over, and checks each stream's
    counters as it ends."""

    def __init__(self, agent: tinyaisense.Session, device: SimDevice, rng: random.Random) -> None:
        self._agent = agent
        self._device = device
        self._name = device.instance
        self._rng = rng
        self.calls = 0
        self.refused: dict[str, int] = {}
        """ApiError codes, and how often each came: the device was off, or busy."""

    async def run(self, until: float) -> None:
        loop = asyncio.get_running_loop()
        ops: list[Callable[[], Awaitable[None]]] = [self.speak, self.say, self.listen, self.led]
        while loop.time() < until:
            self.calls += 1
            op = self._rng.choice(ops)
            try:
                async with asyncio.timeout(STUCK_S):
                    await op()
            except tinyaisense.ApiError as e:
                self.refused[e.code] = self.refused.get(e.code, 0) + 1
                await asyncio.sleep(0.05)
            except TimeoutError:
                raise AssertionError(f"{op.__name__} on {self._name} got stuck") from None

    async def speak(self) -> None:
        sent_ms = self._rng.randrange(100, 600, 20)
        handle = await self._agent.stream_start(f"{self._name}/speaker")
        started = asyncio.get_running_loop().time()
        stream = self._device.speaker.stream  # the hub's id for it on the device
        await self._agent.send_audio(handle, sources.tone(sent_ms))
        if self._rng.random() < 0.5:
            ended = await self._agent.stream_drain(handle)
        else:
            await asyncio.sleep(self._rng.uniform(0, sent_ms / 1000))
            ended = await self._agent.stream_stop(handle)
        stats = ended["stats"]
        assert stats["played_ms"] <= sent_ms, ended
        if ended["reason"] == "drained":  # every frame played, or lost and concealed
            assert stats["played_ms"] == sent_ms, ended
            # As the device heard it: each lost frame concealed by silence, or skipped once
            # an underrun's pause had stood in for it.
            heard = [
                f for f in self._device.speaker.captured if f.stream == stream and f.at >= started
            ]
            heard_ms = sum(len(f.pcm) for f in heard) // BYTES_PER_MS
            frame_ms = len(heard[0].pcm) // BYTES_PER_MS
            skipped = stats["lost"] - sum(f.lost for f in heard)
            assert sent_ms - heard_ms == skipped * frame_ms, (ended, heard_ms, frame_ms)

    async def say(self) -> None:
        said = await self._agent.say(f"{self._name}/speaker", "chaos", replace=True)
        assert said["played_ms"] <= 50, said  # the fake TTS's 10 ms a character

    async def listen(self) -> None:
        handle = await self._agent.stream_start(f"{self._name}/mic")
        received: list[HubAudio] = []

        async def read() -> None:
            received.extend([frame async for frame in self._agent.audio(handle)])

        reader = asyncio.create_task(read())
        try:
            await asyncio.sleep(self._rng.uniform(0.1, 0.4))
            ended = await self._agent.stream_stop(handle)
            await reader
        finally:
            reader.cancel()
        stats = ended["stats"]
        assert stats["frames"] == len(received), ended
        indices = [frame.index for frame in received]
        assert indices == sorted(set(indices)), ended
        if not stats["skipped"]:
            assert indices == list(range(len(indices))), ended

    async def led(self) -> None:
        color = f"#{self._rng.randrange(1 << 24):06x}"
        await self._agent.command(f"{self._name}/led", "set", {"color": color})


async def test_a_seeded_chaos_soak_leaves_no_stuck_session_and_no_task_behind(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    before = asyncio.all_tasks()
    devices: list[SimDevice] = []
    for name, board in DEVICES.items():
        device = await sim.add(board, name)
        await owner.approve(device.device_id, name)
        await device.wait_for(lambda d=device: d.state == "online")
        lossy(device)
        devices.append(device)
    loop = asyncio.get_running_loop()
    until = loop.time() + SOAK_S
    rng = random.Random(SEED)

    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        workloads = [Workload(agent, d, random.Random(f"{SEED}:{d.instance}")) for d in devices]
        await asyncio.gather(chaos(devices, rng, until), *(w.run(until) for w in workloads))
        assert all(w.calls > 5 for w in workloads), [w.calls for w in workloads]
        # Refused only for what a device dropping, or a capability in use, explains.
        refused = {code for w in workloads for code in w.refused}
        assert refused <= {"busy", "disconnected", "timeout"}, [w.refused for w in workloads]

        for device in devices:
            calm(device)
        async with asyncio.timeout(10):
            for device in devices:
                await device.wait_for(lambda d=device: d.state == "online")
            while not all(d["online"] for d in await agent.devices()):  # noqa: ASYNC110 - no change events
                await asyncio.sleep(0.05)

        # Every device still plays and streams its mic for this Session.
        for device in devices:
            handle = await agent.stream_start(f"{device.instance}/speaker")
            await agent.send_audio(handle, sources.tone(200))
            ended = await agent.stream_drain(handle)
            assert (ended["reason"], ended["stats"]["played_ms"]) == ("drained", 200)
            said = await agent.say(f"{device.instance}/speaker", "ok")
            assert not said["interrupted"]
            mic = await agent.stream_start(f"{device.instance}/mic")
            await agent.stream_stop(mic)

    # And a new agent can claim each device, and gets its button.
    async with tinyaisense.connect(hub.agent_url, TOKEN) as fresh:
        notifications = fresh.notifications()
        for device in devices:
            await fresh.command(f"{device.instance}/led", "set", {"color": "#00ff00"})
            assert device.lights() == (Light("#00ff00", 64),)
            device.button.press()
            down = await next_of(notifications, "button.down")
            device.button.release()
            await next_of(notifications, "button.up")
            assert down.params["address"] == f"{device.instance}/button"

    await sim.close()
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(5):
            while asyncio.all_tasks() - before:  # noqa: ASYNC110 - no event for it
                await asyncio.sleep(0.05)
    assert not asyncio.all_tasks() - before
