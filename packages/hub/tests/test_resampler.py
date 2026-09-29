"""The hub's resampler, between the device's 16 kHz and an agent's 16, 24 or 48 kHz, and from
whatever rate a TTS engine speaks at."""

import itertools

import numpy as np
import pytest
from tinyaisense_hub.resampler import Resampler, SoxrResampler, resampler_for

from .signals import db, pcm, samples, snr, steady, tone, vhq

RATES = (16000, 24000, 48000)
PAIRS = [(source, target) for source in RATES for target in RATES if source != target]


def resample(source: int, target: int, audio: bytes) -> np.ndarray:
    """`audio` through a Resampler 10 ms at a time, flushed."""
    resampler = Resampler(source, target)
    frame = source // 100 * 2
    out = b"".join(resampler.process(audio[at : at + frame]) for at in range(0, len(audio), frame))
    return samples(out + resampler.flush())


@pytest.mark.parametrize(("source", "target"), PAIRS)
def test_every_10_ms_in_comes_out_as_10_ms_at_the_target_rate(source: int, target: int) -> None:
    resampler = Resampler(source, target)
    audio = pcm(tone(source, 1000, 500))
    frame = source // 100 * 2

    sizes = [len(resampler.process(audio[at : at + frame])) for at in range(0, len(audio), frame)]

    assert sizes == [target // 100 * 2] * 50


@pytest.mark.parametrize(("source", "target"), PAIRS)
@pytest.mark.parametrize(("fraction", "least_snr"), [(0.0125, 80), (0.125, 80), (0.8, 50)])
def test_the_passband_matches_soxr_vhq_1_ms_late(
    source: int, target: int, fraction: float, least_snr: float
) -> None:
    """Tones up to 0.8 of the lower Nyquist, e.g. 6.4 kHz between 16 and 48 kHz. That high,
    the filter is rolling off: 50 dB is a gain within 0.03 dB of soxr's."""
    audio = pcm(tone(source, fraction * min(source, target) / 2, 500))
    reference = vhq(audio, source, target)

    out = resample(source, target, audio)

    late = target // 1000
    assert len(out) == len(reference) + late
    assert snr(out[late:], reference, target) > least_snr


@pytest.mark.parametrize(
    ("source", "target", "hz"),
    [
        (24000, 16000, 9000),
        (24000, 16000, 10800),
        (48000, 16000, 9000),
        (48000, 16000, 21600),
        (48000, 24000, 13500),
        (48000, 24000, 21600),
    ],
)
def test_what_would_alias_is_gone_as_soxr_vhq_removes_it(source: int, target: int, hz: int) -> None:
    """Tones from 1.125 times the target's Nyquist up to 0.9 of the source's: e.g. from 48 to
    16 kHz, 9 kHz would fold back to 7 kHz."""
    audio = tone(source, hz, 500)
    reference = vhq(pcm(audio), source, target)

    out = resample(source, target, pcm(audio))

    error = steady(out[target // 1000 :][: len(reference)], target) - steady(reference, target)
    assert db(np.mean(error**2) / np.mean(audio**2)) < -80


def above(samples: np.ndarray, rate: int, hz: float) -> float:
    """The share of the power of `samples` above `hz`, in dB."""
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples)))) ** 2
    return db(spectrum[np.fft.rfftfreq(len(samples), 1 / rate) > hz].sum() / spectrum.sum())


@pytest.mark.parametrize(("source", "target"), [(s, t) for s, t in PAIRS if s < t])
def test_upsampling_adds_nothing_above_the_source_nyquist_as_soxr_vhq_adds_nothing(
    source: int, target: int
) -> None:
    """A tone at 0.875 of the source's Nyquist, e.g. 7 kHz from 16 kHz, whose first image
    would sit at 9 kHz."""
    audio = pcm(tone(source, 0.875 * source / 2, 500))

    out = resample(source, target, audio)

    assert above(steady(vhq(audio, source, target), target), target, source / 2) < -80
    assert above(steady(out, target), target, source / 2) < -80


def cut_up(resampler: Resampler | SoxrResampler, audio: bytes) -> bytes:
    """`audio` through `resampler` in pieces of odd sizes, flushed."""
    out, at = b"", 0
    for count in itertools.cycle([1, 0, 7, 333, 1000]):
        out += resampler.process(audio[at : at + 2 * count])
        at += 2 * count
        if at >= len(audio):
            break
    return out + resampler.flush()


@pytest.mark.parametrize(("source", "target"), PAIRS)
def test_the_output_doesnt_depend_on_how_the_input_is_cut(source: int, target: int) -> None:
    audio = pcm(tone(source, 1000, 200))
    whole = Resampler(source, target)
    expected = whole.process(audio) + whole.flush()

    assert cut_up(Resampler(source, target), audio) == expected


def test_an_engines_odd_rate_is_resampled_too_however_its_audio_is_cut() -> None:
    """E.g. Piper's 22.05 kHz, which soxr HQ resamples: it holds some of the audio back, until
    it's flushed."""
    out = cut_up(resampler_for(22050, 16000), pcm(tone(22050, 1000, 500)))

    assert len(out) == 2 * 8000
    assert snr(samples(out), tone(16000, 1000, 500), 16000) > 70
