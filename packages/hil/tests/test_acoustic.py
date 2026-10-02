"""What the acoustic rig makes of its recordings, on synthetic ones: chirps found to the sample,
a tone's end, the onset of sound past a pop, clicks told from clean cuts, and the POD's clock."""

import math

import numpy as np
import pytest
from tinyaisense_hil.acoustic import (
    CLICK_DB,
    Heard,
    MicFrame,
    Timeline,
    chirp,
    click_db,
    find_chirp,
    mouth_to_hub,
    onset,
    tone_end,
)

RATE = 48000


def noise(seconds: float, level: float = 0.001, seed: int = 1) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, level, round(seconds * RATE))


def test_a_chirp_is_found_to_the_sample_under_noise_and_nowhere_else() -> None:
    template = chirp(RATE)
    heard = noise(1.0, level=0.01)
    heard[12_345 : 12_345 + len(template)] += 0.02 * template

    assert find_chirp(heard, template, 0, len(heard)) == 12_345
    assert find_chirp(heard, template, 20_000, len(heard)) is None


def tone(seconds: float, hz: float = 1000.0, level: float = 0.3) -> np.ndarray:
    return level * np.sin(2 * np.pi * hz * np.arange(round(seconds * RATE)) / RATE)


def test_a_tone_ends_in_its_ramp_not_where_the_room_has_stopped_ringing() -> None:
    ramp = round(0.002 * RATE)
    played = tone(0.5)
    played[-ramp:] *= np.linspace(1, 0, ramp)
    # The Echo rings on at -8 dB, falling to -20 dB 10 ms on.
    ringing = tone(0.1, level=0.12) * np.exp(-np.arange(round(0.1 * RATE)) / (0.0072 * RATE))
    heard = np.concatenate([played, ringing, np.zeros(RATE // 10)]) + noise(0.7)

    end = tone_end(heard, RATE, 1000.0, len(played) - RATE // 20, len(heard))

    assert end is not None
    assert len(played) - ramp <= end <= len(played)


def test_sound_starts_where_it_rises_over_the_floor_and_stays_not_at_a_pop() -> None:
    heard = noise(1.0)
    pop, speech = RATE // 2 - 2000, RATE // 2
    heard[pop : pop + 96] += 0.2  # 2 ms
    rise = round(0.005 * RATE)
    voice = tone(0.3, hz=220.0, level=0.1)
    voice[:rise] *= np.linspace(0, 1, rise)
    heard[speech : speech + len(voice)] += voice

    start = onset(heard, RATE, RATE // 4, len(heard))

    assert start is not None
    assert speech <= start <= speech + rise
    assert onset(heard, RATE, speech + len(voice), len(heard)) is None


def test_voiced_speech_starts_though_its_level_dips_between_pitch_pulses() -> None:
    heard = noise(1.0)
    speech, period = RATE // 2, RATE // 200
    t = np.arange(period) / RATE
    pulse = 0.1 * np.sin(2 * np.pi * 2000 * t) * np.exp(-t / 0.001)
    heard[speech : speech + 60 * period] += np.tile(pulse, 60)

    start = onset(heard, RATE, RATE // 4, len(heard))

    assert start is not None
    assert speech - RATE // 2000 <= start <= speech + period  # a level is a ms's, around it


def cut_tone(how: str) -> tuple[np.ndarray, int]:
    """Half a second of a 1 kHz tone, cut `ramped` over 2 ms, `hard` at its peak, or ramped
    with a click 5 ms after; with noise. And where it ends."""
    played = tone(0.5 + 0.25 / 1000)  # to a peak
    if how != "hard":
        ramp = round(0.002 * RATE)
        played[-ramp:] *= np.linspace(1, 0, ramp)
    heard = np.concatenate([played, np.zeros(RATE // 5)])
    heard += noise(1.0)[: len(heard)]
    if how == "clicked":
        click = len(played) + round(0.005 * RATE)
        heard[click : click + 24] += 0.05
    return heard, len(played)


def test_a_cut_clicks_if_energy_over_2_khz_rises_over_the_floor_while_the_tone_played() -> None:
    db = {}
    for how in ("ramped", "hard", "clicked"):
        heard, end = cut_tone(how)
        db[how] = click_db(heard, RATE, end, (end - RATE // 5, end - RATE // 100), hz=1000.0)

    assert db["ramped"] < CLICK_DB
    assert db["hard"] > CLICK_DB + 10
    assert db["clicked"] > CLICK_DB + 10


def test_a_cut_of_speech_clicks_if_energy_over_2_khz_rises_over_the_floor_after_it() -> None:
    ramp = round(0.002 * RATE)
    voice = tone(0.5, hz=220.0) + tone(0.5, hz=3300.0, level=0.05)
    voice[-ramp:] *= np.linspace(1, 0, ramp)
    heard = np.concatenate([voice, np.zeros(RATE // 2)])
    heard += noise(1.0)[: len(heard)]
    end, after = len(voice), (len(voice) + RATE // 10, len(voice) + RATE * 3 // 10)
    clicked = heard.copy()
    clicked[end + 200 : end + 224] += 0.05

    assert click_db(heard, RATE, end, after) < CLICK_DB
    assert click_db(clicked, RATE, end, after) > CLICK_DB + 10


def test_the_pods_clock_is_fitted_through_the_jitter_of_its_adc_times_and_its_drift() -> None:
    rng = np.random.default_rng(2)
    starts = np.arange(0, 60 * RATE, 96)  # a minute, 2 ms a block
    true_ms = 1.79e12 + starts / RATE * 1000 * (1 + 100e-6)  # its crystal runs 100 ppm slow
    adc_ms = true_ms + rng.uniform(-0.5, 0.5, len(starts))

    clock = Timeline.fit(list(zip(starts.tolist(), adc_ms.tolist(), strict=True)))

    for index, ms in ((0, true_ms[0]), (len(starts) * 96 - 96, true_ms[-1])):
        assert abs(clock.at(index) - ms) < 0.05
        assert clock.index(ms) == index


MIC_RATE = 16000


def test_mouth_to_hub_runs_from_the_onset_at_the_pod_to_the_frame_the_chirp_starts_in() -> None:
    # Chirps at these times, epoch ms; the POD hears them 27 ms late, and the hub gets each
    # 10 ms mic frame 85 ms after its end. The third frame with a chirp in it is lost.
    start, chirps = 1.79e12, [1500.0, 2104.3, 2707.9]
    pod, mic = noise(4.0), noise(4.0, seed=3)[: 4 * MIC_RATE]
    for at in chirps:
        i = round((at + 27) * RATE / 1000)
        pod[i : i + len(chirp(RATE))] += 0.05 * chirp(RATE)
        j = round(at * MIC_RATE / 1000)
        mic[j : j + len(chirp(MIC_RATE))] += 0.05 * chirp(MIC_RATE)
    pcm = (mic * 32767).astype("<i2")
    lost = round(chirps[2] * MIC_RATE / 1000) // 160
    frames = [
        MicFrame(f, start + (f + 1) * 10 + 85, pcm[f * 160 : (f + 1) * 160].tobytes())
        for f in range(len(pcm) // 160)
        if f != lost
    ]
    heard = Heard(pod, RATE, Timeline(start, 1000 / RATE), lag_ms=27)
    played = [start + at - 30 for at in [*chirps, 3300.0]]  # the PC speaker's DAC, a bit early

    samples = mouth_to_hub(heard, frames, played)

    expected = [(at // 10 + 1) * 10 + 85 - at for at in chirps]
    expected[2] += 10  # the frame after the lost one
    assert samples == [*(pytest.approx(ms, abs=0.1) for ms in expected), math.inf]
