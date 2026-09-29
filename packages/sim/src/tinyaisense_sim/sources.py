"""Audio to feed a simulated mic: 16 kHz mono s16le PCM, the rate every Board's mic captures at.

`mic.feed(sources.tone(500))` makes the mic hear half a second of 440 Hz.
"""

import math
import sys
import wave
from array import array
from functools import cache
from math import gcd
from operator import mul
from os import PathLike

RATE = 16000
"""The rate every source comes out at."""

_ZEROS = 16
"""How many zero crossings of the sinc each side of the resampler's filter spans."""


def silence(ms: int) -> bytes:
    return bytes(ms * RATE // 1000 * 2)


def tone(ms: int, hz: float = 440, amplitude: float = 0.5) -> bytes:
    """A sine of `hz`, at `amplitude` of full scale."""
    peak = amplitude * 32767
    step = 2 * math.pi * hz / RATE
    return _pack(array("h", (round(peak * math.sin(step * n)) for n in range(ms * RATE // 1000))))


def pcm(data: bytes, rate: int = RATE, channels: int = 1) -> bytes:
    """Raw s16le PCM at `rate`, with `channels` interleaved, mixed down and resampled."""
    if len(data) % (2 * channels):
        raise ValueError(f"{len(data)} bytes isn't a whole number of {channels}-channel samples")
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    if channels > 1:
        samples = array(
            "h",
            (
                round(sum(samples[at : at + channels]) / channels)
                for at in range(0, len(samples), channels)
            ),
        )
    return _pack(_resample(samples, rate, RATE))


def wav(path: str | PathLike[str]) -> bytes:
    """A 16-bit PCM WAV file of any rate and channel count, mixed down and resampled."""
    with wave.open(str(path), "rb") as f:
        if f.getsampwidth() != 2:
            raise ValueError(f"{path}: {8 * f.getsampwidth()}-bit samples; only 16-bit is read")
        return pcm(f.readframes(f.getnframes()), f.getframerate(), f.getnchannels())


def _pack(samples: array[int]) -> bytes:
    if sys.byteorder == "big":
        samples = array("h", samples)
        samples.byteswap()
    return samples.tobytes()


def _resample(samples: array[int], source: int, target: int) -> array[int]:
    """Windowed-sinc resampling from rate `source` to `target`, low-passed below both
    Nyquists."""
    if source == target:
        return samples
    up, down, taps = _filter(source, target)
    width = len(taps[0]) // 2
    padded = [0] * width + list(samples) + [0] * (width + 1)
    out = array("h")
    for n in range(len(samples) * up // down):
        center, phase = divmod(n * down, up)
        # Input samples center-width+1 .. center+width sit at padded[center+1 ..].
        value = sum(map(mul, taps[phase], padded[center + 1 : center + 1 + 2 * width], strict=True))
        out.append(max(-32768, min(32767, round(value))))
    return out


@cache
def _filter(source: int, target: int) -> tuple[int, int, list[list[float]]]:
    """`(up, down, taps)`: output sample n sits at input position n·down/up, and `taps[p]`
    weighs the input around a position whose fraction is p/up."""
    common = gcd(source, target)
    up, down = target // common, source // common
    cutoff = min(1.0, target / source)
    width = math.ceil(_ZEROS / cutoff)
    taps: list[list[float]] = []
    for phase in range(up):
        offset = phase / up
        row = [_kernel(offset - j, cutoff, width) for j in range(-width + 1, width + 1)]
        total = sum(row)
        taps.append([t / total for t in row])  # unity gain at DC in every phase
    return up, down, taps


def _kernel(x: float, cutoff: float, width: int) -> float:
    if abs(x) >= width:
        return 0.0
    sinc = 1.0 if x == 0 else math.sin(math.pi * cutoff * x) / (math.pi * cutoff * x)
    w = x / width
    blackman = 0.42 + 0.5 * math.cos(math.pi * w) + 0.08 * math.cos(2 * math.pi * w)
    return cutoff * sinc * blackman
