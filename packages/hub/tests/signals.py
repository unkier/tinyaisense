"""Test signals, and soxr VHQ as the reference for resampled audio."""

import numpy as np
import soxr


def tone(rate: int, hz: float, ms: int, amplitude: float = 16000) -> np.ndarray:
    n = np.arange(rate * ms // 1000)
    return amplitude * np.sin(2 * np.pi * hz * n / rate)


def pcm(samples: np.ndarray) -> bytes:
    """s16le."""
    return np.round(samples).astype("<i2").tobytes()


def samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, "<i2").astype(np.float64)


def vhq(pcm: bytes, source: int, target: int) -> np.ndarray:
    """What soxr VHQ resamples s16le `pcm` to."""
    return soxr.resample(np.frombuffer(pcm, "<i2"), source, target, "VHQ").astype(np.float64)


def steady(samples: np.ndarray, rate: int) -> np.ndarray:
    """Past the first and last 50 ms, where the filters of both resamplers have settled."""
    return samples[rate // 20 : -rate // 20]


def db(power: float) -> float:
    return float(10 * np.log10(power))


def snr(out: np.ndarray, reference: np.ndarray, rate: int) -> float:
    """How close `out` is to `reference`, both at `rate` from the same start, past the ends:
    the reference's power over the difference's, in dB. `out` may run on past it."""
    error = steady(out[: len(reference)], rate) - steady(reference, rate)
    return db(np.mean(steady(reference, rate) ** 2) / np.mean(error**2))
