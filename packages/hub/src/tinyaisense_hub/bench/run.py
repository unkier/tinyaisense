"""`bench speech`: the model tier's latency gates, timed on the hub the owner configured, which
runs as `serve` runs it, in a process of its own. Simulated Atom Echos stand in for the devices,
and the SDK for the agent, both in this process, on the event loop's clock: run it on asyncio's
own loop, whose clock is the monotonic clock to the ns.

- STT: push-to-talk on each clip of speech, which ends right after its last frame of speech,
  from the release to `utterance.end` with the transcript.
- TTS: `say` of each sentence, from the request to the first frame reaching the device.
- Interrupt, with `load`: presses at random, from each press to `button.down` reaching the
  SDK, while other simulated devices keep the hub's STT and TTS busy, speaking and hearing.
"""

import asyncio
import contextlib
import math
import os
import random
import time
from collections import Counter
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self, cast

import tinyaisense
from tinyaisense_sim import SimDevice, Simulator
from tinyaisense_wire import JsonObject

from ..config import Config
from ..streams import FORMAT
from .corpus import BenchError, Sentence
from .gates import Gate, word_errors
from .hub import HubProcess, hub_version
from .report import Heard, Load, Report, Spoke, machine

BOARD = "m5stack-atom-echo"
STT_GATE = (95, 150)
TTS_GATE = (95, 150)
INTERRUPT_GATE = (99, 20)
"""Each gate's percentile, and its target in ms."""
SAYERS = 2
LISTENERS = 2
"""The simulated devices that load the hub during the interrupt gate."""
GAP_MS = (30.0, 120.0)
HOLD_MS = (20.0, 60.0)
"""How long the interrupt gate waits before each press, and holds it: at random, within."""
TAIL_MS = 1500
"""The silence a loading listen hears after its speech: its turn ends within it."""
ONLINE_S = 5
STT_S = 30
TTS_S = 60
DOWN_S = 1
"""How long a result may take before the bench gives up on it: a lost `button.down` is the
slowest sample."""


@dataclass(frozen=True, slots=True)
class Spoken:
    """A clip of speech, as the device's mic hears it: 16 kHz mono s16le."""

    name: str
    lang: str
    text: str
    pcm: bytes


async def bench(
    config: Config,
    spoken: Sequence[Spoken],
    sentences: Sequence[Sentence],
    *,
    name: str | None = None,
    reps: int = 3,
    load: bool = False,
    presses: int = 1000,
    seed: int | None = None,
    report: Callable[[str], None] = lambda _: None,
) -> Report:
    """Time each gate `reps` times, on the hub `config` makes, and the interrupt gate only with
    `load`, over `presses` presses. The report is `name`d after the hub's commit by default,
    or else `bench`. `report` hears how it goes. Raises `BenchError`."""
    stt = Gate("stt", "STT: last speech frame → final transcript", *STT_GATE)
    tts = Gate("tts", "TTS: text → first 10 ms frame at the device", *TTS_GATE)
    versions, now = hub_version(), datetime.now(UTC)
    result = Report(
        name or _short(cast(str | None, versions.get("commit"))),
        now,
        versions,
        machine(),
        config.speech,
        gates=[stt, tts],
    )
    report(f"loadavg {result.machine['loadavg']}, governor {result.machine['governor']}")
    async with HubProcess(config) as hub, _Rig(hub) as rig:
        device = await rig.device("bench")
        address = cast(str, device.name)
        for rep in range(reps):
            heard = [await rig.stt(device, clip) for clip in spoken]
            result.heard.append(heard)
            stt.runs.append([h.ms for h in heard])
            report(f"stt run {rep + 1}/{reps}: p95 {stt.value(rep):.1f} ms")
        for rep in range(reps):
            spoke = [await rig.tts(device, sentence) for sentence in sentences]
            result.spoke.append(spoke)
            tts.runs.append([s.ms for s in spoke])
            report(f"tts run {rep + 1}/{reps}: p95 {tts.value(rep):.1f} ms")
        if load:
            interrupt = Gate(
                "interrupt",
                "Interrupt: press → `button.down` at the SDK, under STT+TTS load",
                *INTERRUPT_GATE,
            )
            result.gates.append(interrupt)
            await rig.session.reflex_set(address, armed=False)
            sayers = [await rig.device(f"bench-say-{i}") for i in range(SAYERS)]
            listeners = [await rig.device(f"bench-listen-{i}") for i in range(LISTENERS)]
            rng = random.Random(seed)
            for rep in range(reps):
                samples, run = await rig.interrupt(
                    device, presses, rng, sayers, listeners, spoken, sentences
                )
                interrupt.runs.append(samples)
                result.load.append(run)
                report(f"interrupt run {rep + 1}/{reps}: p99 {interrupt.value(rep):.1f} ms")
            result.button_latency = await hub.button_latency()
    result.loadavg_after = os.getloadavg()
    return result


def _short(commit: str | None) -> str:
    if commit is None:
        return "bench"
    head, dirty, _ = commit.partition("-dirty")
    return head[:7] + dirty


@dataclass(eq=False, slots=True)
class _Expected:
    method: str
    match: Callable[[JsonObject], bool]
    got: asyncio.Future[tuple[float, JsonObject]]


class _Rig:
    """The bench's side of the hub: a Simulator of its devices, and an agent's Session, which
    notes when each notification comes in."""

    def __init__(self, hub: HubProcess) -> None:
        self._hub = hub
        self.sim = Simulator(hub.device_address)
        self._session: tinyaisense.Session | None = None
        self._expected: list[_Expected] = []
        self._reader: asyncio.Task[None] | None = None

    @property
    def session(self) -> tinyaisense.Session:
        assert self._session is not None
        return self._session

    async def __aenter__(self) -> Self:
        self._session = await tinyaisense.connect(
            self._hub.agent_url, self._hub.token, utterances={"transcript": True}
        )
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._session is not None:
            await self._session.close()
        if self._reader is not None:
            await self._reader
        await self.sim.close()

    async def _read(self) -> None:
        async for notification in self.session.notifications():
            at = asyncio.get_running_loop().time()
            for expected in list(self._expected):
                if expected.got.done():  # given up on
                    self._expected.remove(expected)
                elif expected.method == notification.method and expected.match(notification.params):
                    self._expected.remove(expected)
                    expected.got.set_result((at, notification.params))
                    break

    def expect(
        self, method: str, match: Callable[[JsonObject], bool] = lambda _: True
    ) -> asyncio.Future[tuple[float, JsonObject]]:
        """When the next notification `method` that `match`es comes in, and its params."""
        got = asyncio.get_running_loop().create_future()
        self._expected.append(_Expected(method, match, got))
        return got

    async def device(self, name: str) -> SimDevice:
        """A new simulated Echo, once it's online and the hub has armed its Reflex."""
        online = self.expect("device.online")
        device = await _within(self.sim.add(BOARD, name), ONLINE_S, f"no welcome of {name}")
        while (await _within(online, ONLINE_S, f"no {name} online"))[1]["device"] != device.name:
            online = self.expect("device.online")
        await _within(device.wait_for(lambda: device.armed), ONLINE_S, f"no Reflex on {name}")
        return device

    async def stt(self, device: SimDevice, clip: Spoken) -> Heard:
        end = self.expect("utterance.end")
        await device.talk(clip.pcm)
        released = asyncio.get_running_loop().time()
        at, params = await _within(end, STT_S, f"no transcript of {clip.name}")
        transcript = cast(str, params.get("transcript", ""))
        errors, words = word_errors(clip.text, transcript)
        return Heard(clip.name, clip.lang, (at - released) * 1000, transcript, errors, words)

    async def tts(self, device: SimDevice, sentence: Sentence) -> Spoke:
        sent = asyncio.get_running_loop().time()
        said = await _within(
            self.session.say(f"{device.name}/speaker", sentence.text, lang=sentence.lang),
            TTS_S,
            f"no say of {sentence.text!r}",
        )
        first = device.speaker.first_frame_at
        if said["reason"] != "drained" or first is None or first < sent:
            raise BenchError(f"the say of {sentence.text!r} didn't play: {said}")
        return Spoke(sentence.lang, sentence.text, (first - sent) * 1000)

    async def interrupt(
        self,
        device: SimDevice,
        presses: int,
        rng: random.Random,
        sayers: Sequence[SimDevice],
        listeners: Sequence[SimDevice],
        spoken: Sequence[Spoken],
        sentences: Sequence[Sentence],
    ) -> tuple[list[float], Load]:
        """Press `device`'s button `presses` times, at random, while `sayers` speak
        `sentences` and `listeners` hear `spoken`. Returns how long each `button.down` took, and
        what the run got."""
        counts = Counter[str]()
        loads = [self._saying(d, sentences, counts) for d in sayers]
        loads += [self._listening(d, spoken, counts) for d in listeners]
        tasks = [asyncio.create_task(load) for load in loads]
        address = f"{device.name}/button"
        samples: list[float] = []
        try:
            for _ in range(presses):
                gap = rng.uniform(*GAP_MS)
                # A press's edges happened on the device after midway through the gap before
                # it, and the last press's before: `t` tells a late edge of the last press apart.
                since = time.time() * 1000 + gap / 2

                def mine(params: JsonObject, since: float = since) -> bool:
                    return params["address"] == address and cast(int, params["t"]) >= since

                await asyncio.sleep(gap / 1000)
                down, up = self.expect("button.down", mine), self.expect("button.up", mine)
                pressed = asyncio.get_running_loop().time()
                device.button.press()
                try:
                    at, _ = await asyncio.wait_for(down, DOWN_S)
                    samples.append((at - pressed) * 1000)
                except TimeoutError:
                    samples.append(math.inf)
                await asyncio.sleep(rng.uniform(*HOLD_MS) / 1000)
                device.button.release()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(up, DOWN_S)
            for task in tasks:
                if task.done() and (e := task.exception()) is not None:
                    raise BenchError(f"the load failed: {e!r}") from e
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        lost = sum(math.isinf(ms) for ms in samples)
        return samples, Load(presses, lost, counts["says"], counts["utterances"])

    async def _saying(
        self, device: SimDevice, sentences: Sequence[Sentence], counts: Counter[str]
    ) -> None:
        while True:
            for sentence in sentences:
                await self.session.say(f"{device.name}/speaker", sentence.text, lang=sentence.lang)
                counts["says"] += 1

    async def _listening(
        self, device: SimDevice, spoken: Sequence[Spoken], counts: Counter[str]
    ) -> None:
        tail = bytes(TAIL_MS * FORMAT.frame_bytes // FORMAT.frame_ms)
        while True:
            for clip in spoken:
                device.mic.forget()
                device.mic.feed(clip.pcm + tail)
                await self.session.listen(f"{device.name}/mic")
                counts["utterances"] += 1


async def _within[T](
    awaitable: asyncio.Future[T] | Coroutine[Any, Any, T], seconds: float, what: str
) -> T:
    """Raises `BenchError` if it takes longer than `seconds`."""
    try:
        return await asyncio.wait_for(awaitable, seconds)
    except TimeoutError:
        raise BenchError(f"{what} within {seconds} s") from None
