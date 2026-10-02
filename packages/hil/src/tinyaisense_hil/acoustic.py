"""What the acoustic rig hears, worked out from its recordings, in numpy: the rig's PC speaker
plays chirps into the Echo's mic as a mouth, and the POD, a USB mic touching the Echo, is the
reference ear for both.

- A chirp is found to the sample by a matched filter, in the POD's recording and in the mic's.
- A tone's end, where the speaker went silent, is where its level halves.
- The onset of sound, e.g. speech, is where it rises over the noise floor and mostly stays
  there, past a pop.
- A click is energy over 2 kHz just after a cut, over the noise floor.
- The POD's clock: when each of its samples was heard, from PortAudio's ADC times."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

CHIRP_MS = 30.0
CHIRP_HZ = (1000.0, 4000.0)
"""A sweep the Echo's speaker and mic both pass, and far from the room's steady 5 kHz."""
MIN_MATCH = 0.4
"""How well a stretch of a recording must match a chirp, by normalized cross-correlation, to
be one: noise alone reaches ~0.1."""


def chirp(rate: int) -> np.ndarray:
    """A linear sweep over `CHIRP_HZ`, `CHIRP_MS` long at `rate`, its ends tapered, peaking at
    1."""
    n = round(rate * CHIRP_MS / 1000)
    t = np.arange(n) / rate
    f0, f1 = CHIRP_HZ
    sweep = np.sin(2 * np.pi * (f0 * t + (f1 - f0) * t * t / (2 * CHIRP_MS / 1000)))
    taper = n // 5
    window = np.hanning(2 * taper)
    sweep[:taper] *= window[:taper]
    sweep[-taper:] *= window[taper:]
    return sweep


def find_chirp(signal: np.ndarray, template: np.ndarray, start: int, stop: int) -> int | None:
    """Where in `signal` the `template` starts, starting in `[start, stop)`: the best match, by
    normalized cross-correlation; None if none matches `MIN_MATCH` or better."""
    n = len(template)
    start = max(0, start)
    segment = signal[start : min(len(signal), stop + n - 1)]
    if len(segment) < n:
        return None
    size = 1 << (len(segment) + n - 1).bit_length()
    product = np.fft.irfft(np.fft.rfft(segment, size) * np.conj(np.fft.rfft(template, size)), size)
    correlation = product[: len(segment) - n + 1]
    energy = np.cumsum(np.concatenate(([0.0], segment.astype(np.float64) ** 2)))
    norms = np.sqrt(np.maximum(energy[n:] - energy[:-n], 1e-30)) * np.linalg.norm(template)
    match = correlation / norms
    best = int(np.argmax(match))
    return start + best if match[best] >= MIN_MATCH else None


TONE_DROP_DB = 6.0
"""How far a tone's level falls where it ends: halfway down its ramp. The Echo rings on after,
~8 dB down, for ~10 ms."""
LEVEL_MS = 100.0
"""How long before where a tone's end is looked for its level is taken over."""
_DROP = 10 ** (-TONE_DROP_DB / 20)


def tone_end(signal: np.ndarray, rate: int, hz: float, start: int, stop: int) -> int | None:
    """Where the `hz` tone playing up to `start` in `signal` ends, in `[start, stop)`: where its
    level first falls `TONE_DROP_DB` under its level just before `start`. None if it doesn't."""
    before = max(0, start - round(rate * LEVEL_MS / 1000))
    level = tone_level(signal[before:stop], rate, hz)
    (ended,) = np.nonzero(level[start - before :] < np.median(level[: start - before]) * _DROP)
    return start + int(ended[0]) if len(ended) else None


def tone_level(signal: np.ndarray, rate: int, hz: float) -> np.ndarray:
    """The amplitude of the `hz` tone in `signal`, at each sample: over one period around it, so
    that its harmonics don't count."""
    period = max(1, round(rate / hz))
    t = np.arange(len(signal)) / rate
    base = signal * np.exp(-2j * np.pi * hz * t)
    return 2 * np.abs(np.convolve(base, np.ones(period) / period, "same"))


FLOOR_MS = 200.0
"""How long before where sound is looked for the noise floor is taken over."""
ONSET_DB = 6.0
"""How far over the noise floor sound starts."""
SUSTAIN_MS = 10.0
"""How long sound must stay over `ONSET_DB`, for most of it, to have started: voiced speech
dips between its pitch pulses, and a pop, e.g. the amplifier's as it turns on, is over sooner."""


def onset(signal: np.ndarray, rate: int, start: int, stop: int) -> int | None:
    """Where sound starts in `signal`, in `[start, stop)`: where its level rises `ONSET_DB` over
    the noise floor of the `FLOOR_MS` before `start`, and stays there for most of the
    `SUSTAIN_MS` after. None if it doesn't."""
    before = max(0, start - round(rate * FLOOR_MS / 1000))
    level = envelope(signal[before:stop], rate)
    over = level[start - before :] > _floor(level[: start - before]) * 10 ** (ONSET_DB / 20)
    sustain = round(rate * SUSTAIN_MS / 1000)
    held = 2 * np.convolve(over, np.ones(sustain, dtype=int), "valid") > sustain
    (starts,) = np.nonzero(over[: len(held)] & held)
    return start + int(starts[0]) if len(starts) else None


def envelope(signal: np.ndarray, rate: int) -> np.ndarray:
    """The level of `signal` at each sample: its RMS over the ms around it."""
    ms = max(1, round(rate / 1000))
    return np.sqrt(np.convolve(np.square(signal), np.ones(ms) / ms, "same"))


def _floor(level: np.ndarray) -> float:
    """The noise floor of a stretch of `level`s: its 99th percentile, as high as noise goes."""
    return float(np.percentile(level, 99))


CLICK_HZ = 2000.0
"""A click is energy over this: a cut tone, or speech, has little of it once ramped out."""
CLICK_DB = 6.0
"""How far over the noise floor energy over `CLICK_HZ` may peak, just after a cut, without
being a click: a ramped cut's reached 2.3 dB on the rig, a stopped channel's 12 dB and up."""
CLICK_MS = 20.0
"""How long after a cut a click is looked for."""


def click_db(
    signal: np.ndarray,
    rate: int,
    after: int,
    floor: tuple[int, int],
    hz: float | None = None,
    ms: float = CLICK_MS,
) -> float:
    """How far, in dB, the energy over `CLICK_HZ` in the `ms` after `after` in `signal` peaks
    over the noise floor in `signal[floor]`: its highest level, a ms's, against the floor's.

    With `hz`, a tone at `hz` and its harmonics are notched out first, so that the floor may be
    taken while it plays, and its ringing in the room isn't a click. The notch remembers a
    period of it, so the `ms` start a period later, and half a ms later in any case: a ms's
    level is taken around each sample."""
    period = 0 if hz is None else round(rate / hz)
    start = after + period + rate // 2000
    stop = start + round(rate * ms / 1000)
    offset = max(0, min(start, floor[0]) - rate // 100)
    part = signal[offset : max(stop, floor[1]) + rate // 100].astype(np.float64)
    if period:
        part = part[period:] - part[:-period]
        offset += period
    spectrum = np.fft.rfft(part)
    spectrum[np.fft.rfftfreq(len(part), 1 / rate) < CLICK_HZ] = 0
    level = envelope(np.fft.irfft(spectrum, len(part)), rate)
    peak = level[start - offset : stop - offset].max()
    return float(20 * np.log10(peak / _floor(level[floor[0] - offset : floor[1] - offset])))


@dataclass(frozen=True, slots=True)
class Timeline:
    """When each sample of a recording was heard, in epoch ms: a line through PortAudio's ADC
    times of its blocks, which jitter with the callbacks, at the rate the recording's crystal
    runs at, which drifts from the PC's."""

    start_ms: float
    """When its first sample was heard."""
    ms_per_sample: float

    @classmethod
    def fit(cls, blocks: Sequence[tuple[int, float]]) -> Timeline:
        """The line through `blocks`: each one's first sample, and when it was heard, by its
        ADC time."""
        samples = np.array([index for index, _ in blocks], dtype=np.float64)
        times = np.array([ms for _, ms in blocks], dtype=np.float64)
        mid_sample, mid_ms = samples.mean(), times.mean()
        slope = np.dot(samples - mid_sample, times - mid_ms) / np.dot(
            samples - mid_sample, samples - mid_sample
        )
        return cls(float(mid_ms - slope * mid_sample), float(slope))

    def at(self, index: float) -> float:
        """When the sample at `index` was heard."""
        return self.start_ms + index * self.ms_per_sample

    def index(self, ms: float) -> int:
        """The sample heard at `ms`."""
        return round((ms - self.start_ms) / self.ms_per_sample)


@dataclass(frozen=True, slots=True)
class Heard:
    """A recording at `rate`, and when each of its samples was heard, in epoch ms, by its
    `clock`, with its recorder's `lag_ms` taken off: how much later it hears than its ADC times
    say."""

    signal: np.ndarray
    rate: int
    clock: Timeline
    lag_ms: float = 0.0

    def at(self, index: float) -> float:
        """When the sample at `index` was heard."""
        return self.clock.at(index) - self.lag_ms

    def index(self, ms: float) -> int:
        """The sample heard at `ms`."""
        return self.clock.index(ms + self.lag_ms)


@dataclass(frozen=True, slots=True)
class MicFrame:
    """A frame of a raw mic stream: its `index` on the stream's timeline, when the agent got it,
    in epoch ms, and its PCM, s16le."""

    index: int
    got_ms: float
    pcm: bytes


MIC_RATE = 16000
MIC_FRAME = MIC_RATE // 100
"""Samples in a 10 ms mic frame."""
SEARCH_MS = (50.0, 400.0)
"""How long before a chirp was played, and after, the POD may hear it."""
REACH_MS = 400.0
"""How long after the POD heard a chirp the frame it starts in may reach the agent before
it counts as lost: it's the next chirp's after that."""


def mouth_to_hub(
    heard: Heard, frames: Sequence[MicFrame], played_ms: Sequence[float]
) -> list[float]:
    """From each chirp played at `played_ms` onto the Echo's mic, by when the POD `heard` it
    start, to when the agent got the first mic frame from the one it starts in, in ms; `inf`
    for a chirp not found in either."""
    if not frames:
        return [math.inf] * len(played_ms)
    count = max(frame.index for frame in frames) + 1
    mic = np.zeros(count * MIC_FRAME)
    got = np.full(count, math.inf)
    for frame in frames:
        pcm = np.frombuffer(frame.pcm, "<i2")
        mic[frame.index * MIC_FRAME : frame.index * MIC_FRAME + len(pcm)] = pcm / 32768
        got[frame.index] = frame.got_ms
    # The first frame got from each on, for a lost one.
    reached = np.minimum.accumulate(got[::-1])[::-1]
    at_pod, at_mic = chirp(heard.rate), chirp(MIC_RATE)
    samples: list[float] = []
    for played in played_ms:
        start = find_chirp(
            heard.signal,
            at_pod,
            heard.index(played - SEARCH_MS[0]),
            heard.index(played + SEARCH_MS[1]),
        )
        if start is None:
            samples.append(math.inf)
            continue
        onset_ms = heard.at(start)
        (window,) = np.nonzero((got >= onset_ms) & (got < onset_ms + REACH_MS))
        found = (
            find_chirp(mic, at_mic, int(window[0]) * MIC_FRAME, (int(window[-1]) + 1) * MIC_FRAME)
            if len(window)
            else None
        )
        samples.append(math.inf if found is None else float(reached[found // MIC_FRAME]) - onset_ms)
    return samples
