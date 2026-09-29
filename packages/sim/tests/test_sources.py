"""Audio sources for the simulated mic: 16 kHz mono s16le."""

import math
import struct
import wave
from pathlib import Path

import pytest
from tinyaisense_sim import sources


def samples(pcm: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(pcm) // 2}h", pcm))


def sine(hz: float, rate: int, seconds: float, amplitude: float = 0.5) -> list[int]:
    return [
        round(amplitude * 32767 * math.sin(2 * math.pi * hz * n / rate))
        for n in range(round(rate * seconds))
    ]


def power_at(pcm: bytes, hz: float) -> float:
    """The mean power of `pcm` at `hz`, relative to full scale: a single DFT bin."""
    x = samples(pcm)
    re = sum(v * math.cos(2 * math.pi * hz * n / 16000) for n, v in enumerate(x))
    im = sum(v * math.sin(2 * math.pi * hz * n / 16000) for n, v in enumerate(x))
    return (re**2 + im**2) / (len(x) * 32767) ** 2


def write_wav(path: Path, rate: int, channels: list[list[int]]) -> None:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(len(channels))
        f.setsampwidth(2)
        f.setframerate(rate)
        frames = [v for frame in zip(*channels, strict=True) for v in frame]
        f.writeframes(struct.pack(f"<{len(frames)}h", *frames))


def test_silence_is_zeros_of_the_length_asked() -> None:
    assert sources.silence(20) == bytes(640)


def test_a_tone_is_a_sine_of_the_frequency_and_amplitude_asked() -> None:
    tone = sources.tone(100, hz=1000, amplitude=0.25)

    assert len(tone) == 3200
    assert samples(tone) == sine(1000, 16000, 0.1, 0.25)


def test_16_khz_mono_pcm_passes_through_untouched() -> None:
    pcm = sources.tone(30)

    assert sources.pcm(pcm) == pcm


def test_stereo_pcm_is_mixed_down() -> None:
    stereo = struct.pack("<4h", 100, 300, -50, -150)

    assert samples(sources.pcm(stereo, channels=2)) == [200, -100]


def test_pcm_that_isnt_whole_samples_is_refused() -> None:
    with pytest.raises(ValueError, match="whole number"):
        sources.pcm(bytes(3), channels=2)


@pytest.mark.parametrize("rate", [8000, 22050, 44100, 48000])
def test_a_wav_at_another_rate_is_resampled_to_16_khz_keeping_its_pitch(
    tmp_path: Path, rate: int
) -> None:
    path = tmp_path / "tone.wav"
    write_wav(path, rate, [sine(1000, rate, 0.2)])

    pcm = sources.wav(path)

    assert len(pcm) == 0.2 * 16000 * 2
    middle = pcm[800:-800]  # away from the filter's edges
    # A 0.5 amplitude sine has a power of 0.0625 at its own frequency, and next to none
    # elsewhere.
    assert power_at(middle, 1000) == pytest.approx(0.0625, rel=0.02)
    assert power_at(middle, 3000) < 1e-6


def test_resampling_down_filters_out_what_16_khz_cant_carry(tmp_path: Path) -> None:
    path = tmp_path / "high.wav"
    write_wav(path, 48000, [sine(12000, 48000, 0.2)])  # above the 8 kHz Nyquist

    pcm = sources.wav(path)

    # Unfiltered, 12 kHz would alias onto 4 kHz.
    assert power_at(pcm[800:-800], 4000) < 1e-5


def test_a_stereo_wav_is_mixed_down(tmp_path: Path) -> None:
    path = tmp_path / "stereo.wav"
    left = sine(500, 16000, 0.05)
    write_wav(path, 16000, [left, [-v for v in left]])

    assert sources.wav(path) == bytes(1600)


def test_a_wav_that_isnt_16_bit_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "8bit.wav"
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(1)
        f.setframerate(16000)
        f.writeframes(bytes(100))

    with pytest.raises(ValueError, match="16-bit"):
        sources.wav(path)
