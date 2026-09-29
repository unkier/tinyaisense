"""Agent streams at 24 and 48 kHz, which the hub resamples to and from the device's 16 kHz:
docs/protocol/agent.md#streams."""

import pytest
import tinyaisense
from tinyaisense_hub import Hub, OwnerClient
from tinyaisense_sim import SimDevice, Simulator, sources

from .signals import pcm, samples, snr, tone, vhq

ECHO = "m5stack-atom-echo"
TOKEN = "agent-token"
"""The `start_hub` fixture's agent token."""


async def paired(sim: Simulator, owner: OwnerClient, name: str = "kitchen") -> SimDevice:
    device = await sim.add(ECHO, name)
    await owner.approve(device.device_id, name)
    await device.wait_for(lambda: device.state == "online")
    return device


async def test_a_48_khz_speaker_stream_plays_on_the_device_at_16_khz_1_ms_late(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    audio = pcm(tone(48000, 1000, 300))
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        handle = await agent.stream_start("kitchen/speaker", rate=48000)
        await agent.send_audio(handle, audio)

        ended = await agent.stream_drain(handle)

    assert ended["reason"] == "drained"
    heard = samples(kitchen.speaker.pcm())
    reference = vhq(audio, 48000, 16000)
    assert len(heard) == len(reference) + 16  # all of it, 1 ms late
    assert ended["stats"]["played_ms"] == 301
    assert snr(heard[16:], reference, 16000) > 80


async def test_a_24_khz_mic_stream_reaches_the_agent_as_10_ms_frames_1_ms_late(
    hub: Hub, sim: Simulator, owner: OwnerClient
) -> None:
    kitchen = await paired(sim, owner)
    heard = sources.tone(300)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        handle = await agent.stream_start("kitchen/mic", rate=24000)
        frames = agent.audio(handle)
        kitchen.mic.feed(heard, burst=True)
        received = [await anext(frames) for _ in range(40)]

        ended = await agent.stream_stop(handle)
        received += [f async for f in frames]
        started = await anext(notifications)

    assert started.params["rate"] == 24000
    assert [f.index for f in received] == list(range(len(received)))
    assert {len(f.pcm) for f in received} == {480}
    assert ended["stats"]["frames"] == len(received)
    # The tone starts on a frame of the device's: on that frame here, 1 ms (24 samples) in.
    first = next(n for n, f in enumerate(received) if any(f.pcm))
    timeline = samples(b"".join(f.pcm for f in received))
    at = first * 240 + 24
    assert snr(timeline[at:], vhq(heard, 16000, 24000), 24000) > 80


@pytest.mark.parametrize("address", ["kitchen/speaker", "kitchen/mic"])
@pytest.mark.parametrize("rate", [16000, 24000, 48000])
async def test_stream_start_takes_each_agent_rate_and_stream_started_names_it(
    hub: Hub, sim: Simulator, owner: OwnerClient, address: str, rate: int
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        notifications = agent.notifications()
        handle = await agent.stream_start(address, rate=rate)
        await agent.stream_stop(handle)

        started = await anext(notifications)

    assert started.method == "stream.started"
    assert started.params["rate"] == rate


@pytest.mark.parametrize("address", ["kitchen/speaker", "kitchen/mic"])
@pytest.mark.parametrize("rate", [8000, 22050, 44100, 96000])
async def test_stream_start_refuses_any_other_rate(
    hub: Hub, sim: Simulator, owner: OwnerClient, address: str, rate: int
) -> None:
    await paired(sim, owner)
    async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
        with pytest.raises(tinyaisense.ApiError) as refused:
            await agent.stream_start(address, rate=rate)

    assert refused.value.code == "invalid_args"
