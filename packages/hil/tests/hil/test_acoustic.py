"""The acoustic rig on the real Echo: what the human hears and says, by the POD, a USB mic touching
the Echo, as the reference ear, and the rig's PC speaker by the Echo's mic as a mouth. Each gate
in 2 of 3 runs:

- mouth→hub: from a chirp's onset at the POD to the agent getting the mic frame it starts in,
  p99 within 150 ms;
- hub→speaker: from a `say`, real TTS, to its onset at the POD, p99 within 200 ms;
- the speaker cut: from a press to silence, by the firmware's timing, p99 within 7 ms;
- the cut click: no energy over 2 kHz after the ramp of a tone's cut, while the check hears the
  click of a cut by stopping the I2S channel, its positive control.

The POD hears later than its ADC times say. It's calibrated first, by Reflex cuts of a tone: from
when the Echo went silent, by its own timing, to when the POD heard it."""

import asyncio
import contextlib
import dataclasses
import math
import random
from dataclasses import dataclass
from typing import Literal

import pytest
import tinyaisense
from tinyaisense_hil.acoustic import (
    MIC_RATE,
    Heard,
    chirp,
    click_db,
    find_chirp,
    mouth_to_hub,
    onset,
    tone_end,
)
from tinyaisense_hil.checks import CutSample, Edge
from tinyaisense_hil.console import Cut
from tinyaisense_hil.hub import HilHub, now_ms
from tinyaisense_hil.pod import RATE, Mouth, Pod
from tinyaisense_hil.report import (
    Calibration,
    HilReport,
    HubToSpeaker,
    MouthToHub,
    SpeakerCut,
)
from tinyaisense_hil.rig import Rig
from tinyaisense_hub.bench.gates import Gate
from tinyaisense_hub.config import LOCAL_SPEECH

from .conftest import TIMEOUT_S, Hil

pytestmark = [pytest.mark.hil, pytest.mark.timeout(TIMEOUT_S)]

RUNS = 3
TTS = dataclasses.replace(
    LOCAL_SPEECH,
    engines={name: LOCAL_SPEECH.engines[name] for name in ("piper", "silero_tts")},
    stt={},
    vad=None,
    turn=None,
)
"""The local TTS engines, and no STT: a press opens the mic for push-to-talk all the same."""
EN, RU = "lessac", "xenia"

TONE_HZ = 1000.0
TONE_LEVEL = 0.5
"""-6 dBFS, as the `tone` hook plays it, the positive control."""
PLAY_MS = 10_000
PLAYED_S = (0.4, 0.7)
"""How long a tone plays before it's cut: past its start, and the noise floor's stretch."""
SPEECH = (
    "The quick brown fox jumps over the lazy dog, and then it runs all the way back home, "
    "where it sleeps until the morning comes."
)
SPEAKING_S = (0.6, 1.6)
"""How long after a `say` of `SPEECH` it's cut: it's speaking by then, and still is."""
HOLD_S = (0.03, 0.06)
"""How long a press is held: up before a speech cut's noise floor is taken after it."""
QUIET_S = 0.4
"""How long it's quiet after each cut, or `say`: the noise floor of what comes next."""
CUT_LOG_S = 1.0
"""How long the speaker may take to log a cut."""
END_SEARCH_MS = (5.0, 300.0)
"""Where a tone's end is looked for, around its cut: hardly before, where the tone may have
paused for an underrun."""
TONE_FLOOR_MS = (150.0, 10.0)
"""Where a tone cut's noise floor is, before its end: while the tone played, notched out."""
SPEECH_FLOOR_MS = (100.0, 300.0)
"""Where a speech cut's noise floor is, after its end."""

CALIBRATION_CUTS = 20
CHIRPS = 100
MOUTH_LEVEL = 0.3
CHIRP_GAP_S = (0.5, 0.7)
"""Between chirps: longer than a chirp may take to reach the agent and still count."""
MIC_WARM_S = 0.5
SAYS = 100
SAY_EN = ["Okay.", "All set.", "On it.", "I'm here.", "Understood."]
SAY_RU = ["Хорошо.", "Готово.", "Слушаю.", "Сделано.", "Понятно."]
REPORTED = 30
"""`say`s in RU, and raw PCM streams: reported, not gated."""
SAY_GAP_S = (0.3, 0.5)
ONSET_WITHIN_MS = 1000.0
START_FLOOR_MS = (200.0, 10.0)
"""Where a raw PCM stream's noise floor is, before its `stream.start`."""
START_CLICK_GAP_MS = 2.0
"""How long before a raw PCM stream's onset its start click is looked for until: past it, the
chirp is over 2 kHz itself."""
CUTS = 100
"""A run's: tone and speech by turns."""
CONTROL_CUTS = 5
"""A run's cuts by stopping the I2S channel: the click check's positive control."""


@dataclass(frozen=True, slots=True)
class Press:
    """A Reflex cut by an injected press, while the speaker played a `tone` or `speech`: the
    press's `down` edge, and the cut as the speaker logged it, None if it didn't."""

    playing: Literal["tone", "speech"]
    down: Edge
    cut: Cut | None

    @property
    def silent_ms(self) -> float | None:
        """When the Echo went silent, in epoch ms, by its press's `t` and its own timing."""
        if self.down.t is None or self.cut is None:
            return None
        return self.down.t + self.cut.silent_us / 1000


@pytest.fixture(scope="session")
def pod(rig: Rig) -> str:
    """The POD, as PortAudio names it."""
    if rig.pod is None:
        pytest.fail(
            "the acoustic rig needs the POD in the rig config, as PortAudio names its ALSA "
            'hardware device: e.g. pod = "Wavo POD: USB Audio"',
            pytrace=False,
        )
    return rig.pod


@pytest.fixture(scope="session")
def calibration(hil: Hil, report: HilReport, pod: str) -> Calibration:
    """The POD's lag, by Reflex cuts of a tone: from when the Echo went silent to when the POD
    heard it."""
    calibration = report.calibration = asyncio.run(_calibrate(hil, pod))
    if not calibration.passed:
        pytest.fail(f"the POD can't be calibrated: {calibration.findings}", pytrace=False)
    return calibration


async def _calibrate(hil: Hil, device: str) -> Calibration:
    rng = random.Random()
    with Pod(device) as pod:
        async with hil.hub() as hub:
            presses = [await _tone_cut(hil, hub, rng) for _ in range(CALIBRATION_CUTS)]
        heard = pod.heard()
    lags: list[float] = []
    for press in presses:
        end, silent = _tone_end(heard, press.down.sent), press.silent_ms
        lags.append(math.inf if end is None or silent is None else heard.at(end) - silent)
    return Calibration(lags)


async def test_mouth_to_hub_within_150_ms_p99(
    hil: Hil, rig: Rig, pod: str, calibration: Calibration, report: HilReport
) -> None:
    gate = Gate("mouth_to_hub", "mouth→hub: chirp onset at the POD → mic frame", 99, 150)
    rng = random.Random()
    sound = MOUTH_LEVEL * chirp(RATE)
    with Pod(pod) as ear, Mouth(rig.mouth) as mouth:
        async with hil.hub() as hub:
            for _ in range(RUNS):
                async with hub.mic() as frames:
                    await asyncio.sleep(MIC_WARM_S)
                    played: list[float] = []
                    for _ in range(CHIRPS):
                        await asyncio.sleep(rng.uniform(*CHIRP_GAP_S))
                        played.append(now_ms())
                        mouth.play(sound)
                    await asyncio.sleep(max(CHIRP_GAP_S))
                gate.runs.append(mouth_to_hub(ear.heard(calibration.lag_ms), frames, played))

    report.mouth_to_hub = MouthToHub(gate, calibration.lag_ms)

    assert report.mouth_to_hub.passed, (
        f"p99 by run: {[round(gate.value(i), 1) for i in range(RUNS)]} ms"
    )


async def test_hub_to_speaker_within_200_ms_p99(
    hil: Hil, pod: str, calibration: Calibration, report: HilReport
) -> None:
    gate = Gate("hub_to_speaker", f"hub→speaker: `say` in {EN} → acoustic onset", 99, 200)
    rng = random.Random()
    with Pod(pod) as ear:
        async with hil.hub(TTS) as hub:
            # Each engine's first `say` warms it up: reported, not gated.
            cold = [await _says(hub, EN, SAY_EN, rng, 1), await _says(hub, RU, SAY_RU, rng, 1)]
            runs = [await _says(hub, EN, SAY_EN, rng) for _ in range(RUNS)]
            ru = await _says(hub, RU, SAY_RU, rng, REPORTED)
            started = await _raw_streams(hub, rng)
        heard = ear.heard(calibration.lag_ms)
    gate.runs.extend(_onsets(heard, sent) for sent in runs)
    raw, clicks = _raw_onsets(heard, started)

    report.hub_to_speaker = HubToSpeaker(
        gate,
        EN,
        {
            f"the first `say` in {EN} and {RU}, cold → onset": _onsets(heard, [*cold[0], *cold[1]]),
            f"`say` in {RU} (ru) → onset": _onsets(heard, ru),
            "raw PCM: `stream.start` → onset": raw,
        },
        clicks,
    )

    assert report.hub_to_speaker.passed, (
        f"p99 by run: {[round(gate.value(i), 1) for i in range(RUNS)]} ms"
    )


async def test_the_speaker_cut_is_silent_within_7_ms_p99_and_doesnt_click(
    hil: Hil, pod: str, calibration: Calibration, report: HilReport
) -> None:
    gate = Gate("speaker_cut", "Speaker cut: press → silent, by the firmware", 99, 7)
    rng = random.Random()
    runs: list[list[CutSample]] = []
    control: list[float] = []
    with Pod(pod) as ear:
        async with hil.hub(TTS) as hub:
            for _ in range(RUNS):
                presses = [
                    await (_tone_cut if i % 2 == 0 else _speech_cut)(hil, hub, rng)
                    for i in range(CUTS)
                ]
                controls = [await _control_cut(hil, rng) for _ in range(CONTROL_CUTS)]
                heard = ear.heard(calibration.lag_ms)
                runs.append([_cut_sample(heard, press) for press in presses])
                gate.runs.append([cut.silent_ms for cut in runs[-1]])
                control += [_control_click(heard, at) for at in controls]

    report.speaker_cut = SpeakerCut(gate, runs, control)

    assert report.speaker_cut.passed, (
        f"p99 by run: {[round(gate.value(i), 2) for i in range(RUNS)]} ms; "
        f"findings: {report.speaker_cut.findings}"
    )


async def _press(
    hil: Hil, hub: HilHub, playing: Literal["tone", "speech"], rng: random.Random
) -> Press:
    """Press and let go, cutting the speaker by the Reflex, and wait for the quiet after."""
    taken = hil.console.cuts
    down = await hub.got("down", hil.console.press("down"))
    cut = await asyncio.to_thread(hil.console.cut, taken, CUT_LOG_S)
    await asyncio.sleep(rng.uniform(*HOLD_S))
    await hub.got("up", hil.console.press("up"))
    await asyncio.sleep(QUIET_S)
    return Press(playing, down, cut if cut is not None and cut.by_reflex else None)


async def _tone_cut(hil: Hil, hub: HilHub, rng: random.Random) -> Press:
    speaker = await hub.start_tone(PLAY_MS, TONE_LEVEL)
    await asyncio.sleep(rng.uniform(*PLAYED_S))
    press = await _press(hil, hub, "tone", rng)
    with contextlib.suppress(tinyaisense.ApiError):  # the press cut it
        await hub.session.stream_stop(speaker)
    return press


async def _speech_cut(hil: Hil, hub: HilHub, rng: random.Random) -> Press:
    say = asyncio.create_task(hub.session.say(f"{hub.device}/speaker", SPEECH, voice=EN))
    await asyncio.sleep(rng.uniform(*SPEAKING_S))
    press = await _press(hil, hub, "speech", rng)
    await say
    return press


async def _control_cut(hil: Hil, rng: random.Random) -> float:
    """Play the `tone` hook's tone and cut it by stopping the I2S channel, which clicks. Returns
    when the cut went out, in epoch ms."""
    await asyncio.to_thread(hil.console.run, f"tone {TONE_HZ:.0f}")
    await asyncio.sleep(rng.uniform(*PLAYED_S))
    at = now_ms()
    await asyncio.to_thread(hil.console.run, "cut disable")
    await asyncio.sleep(QUIET_S)
    return at


def _tone_end(heard: Heard, cut_ms: float) -> int | None:
    """Where the POD heard the tone cut at `cut_ms` end."""
    return tone_end(
        heard.signal,
        heard.rate,
        TONE_HZ,
        heard.index(cut_ms - END_SEARCH_MS[0]),
        heard.index(cut_ms + END_SEARCH_MS[1]),
    )


def _tone_click(heard: Heard, end: int) -> float:
    """How far the tone that ended at `end` clicked, against the floor while it played."""
    floor = (end - _samples(heard, TONE_FLOOR_MS[0]), end - _samples(heard, TONE_FLOOR_MS[1]))
    return click_db(heard.signal, heard.rate, end, floor, hz=TONE_HZ)


def _samples(heard: Heard, ms: float) -> int:
    """How many samples `ms` holds."""
    return round(heard.rate * ms / 1000)


def _cut_sample(heard: Heard, press: Press) -> CutSample:
    silent_ms = math.inf if press.cut is None else press.cut.silent_us / 1000
    if press.playing == "tone":
        end = _tone_end(heard, press.down.sent)
        if end is None:
            return CutSample("tone", silent_ms, None, math.inf)
        heard_ms = None if press.down.t is None else heard.at(end) - press.down.t
        return CutSample("tone", silent_ms, heard_ms, _tone_click(heard, end))
    if (silent := press.silent_ms) is None:
        return CutSample("speech", silent_ms, None, math.inf)
    end = heard.index(silent)
    floor = (end + _samples(heard, SPEECH_FLOOR_MS[0]), end + _samples(heard, SPEECH_FLOOR_MS[1]))
    return CutSample("speech", silent_ms, None, click_db(heard.signal, heard.rate, end, floor))


def _control_click(heard: Heard, at: float) -> float:
    end = _tone_end(heard, at)
    return math.inf if end is None else _tone_click(heard, end)


async def _says(
    hub: HilHub, voice: str, texts: list[str], rng: random.Random, count: int = SAYS
) -> list[float]:
    """Say `count` of `texts` by turns, each once the one before has played. Returns when each
    was sent, in epoch ms."""
    sent: list[float] = []
    for i in range(count):
        await asyncio.sleep(rng.uniform(*SAY_GAP_S))
        sent.append(now_ms())
        await hub.session.say(f"{hub.device}/speaker", texts[i % len(texts)], voice=voice)
    await asyncio.sleep(QUIET_S)
    return sent


def _onsets(heard: Heard, sent: list[float]) -> list[float]:
    """From each `say` sent to its onset at the POD, in ms; `inf` for one not heard."""
    samples: list[float] = []
    for at in sent:
        start = onset(heard.signal, heard.rate, heard.index(at), heard.index(at + ONSET_WITHIN_MS))
        samples.append(math.inf if start is None else heard.at(start) - at)
    return samples


async def _raw_streams(hub: HilHub, rng: random.Random) -> list[float]:
    """Play a chirp on `REPORTED` raw PCM streams, each once the one before has played. Returns
    when each `stream.start` was sent, in epoch ms."""
    pcm = (0.5 * 32767 * chirp(MIC_RATE)).astype("<i2").tobytes()
    started: list[float] = []
    for _ in range(REPORTED):
        await asyncio.sleep(rng.uniform(*SAY_GAP_S))
        started.append(now_ms())
        handle = await hub.session.stream_start(f"{hub.device}/speaker")
        await hub.session.send_audio(handle, pcm)
        await hub.session.stream_drain(handle)
    await asyncio.sleep(QUIET_S)
    return started


def _raw_onsets(heard: Heard, started: list[float]) -> tuple[list[float], list[float]]:
    """From each raw PCM stream's `stream.start` to its chirp's onset at the POD, in ms; and how
    far its start clicked, before the onset, in dB over the noise floor before it. Each chirp is
    looked for before the next stream's start: they're all alike."""
    template = chirp(heard.rate)
    samples: list[float] = []
    clicks: list[float] = []
    for at, next_at in zip(started, [*started[1:], math.inf], strict=True):
        begin = heard.index(at)
        stop = heard.index(min(at + ONSET_WITHIN_MS, next_at))
        found = find_chirp(heard.signal, template, begin, stop)
        if found is None:
            samples.append(math.inf)
            continue
        samples.append(heard.at(found) - at)
        floor = (
            begin - _samples(heard, START_FLOOR_MS[0]),
            begin - _samples(heard, START_FLOOR_MS[1]),
        )
        before_ms = heard.at(found) - at - START_CLICK_GAP_MS
        if before_ms > 0:
            clicks.append(click_db(heard.signal, heard.rate, begin, floor, ms=before_ms))
    return samples, clicks
