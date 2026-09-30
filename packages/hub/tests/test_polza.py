"""The polza.ai speech engines, against a fake polza.ai: what they ask it, and what they make of
its replies."""

import base64
import re
import time

import pytest
from tinyaisense_hub.speech import Transcript, fake
from tinyaisense_hub.speech.polza import PolzaError, PolzaStt, PolzaTts

from .fake_polza import KEY, RATE, REGISTER, FakePolza, pcm_of, wav


def speak(tts: PolzaTts, text: str, voice: str = "nova") -> bytes:
    return b"".join(tts.synthesize(text, voice))


def test_the_tts_speaks_the_text_in_the_voice_asked_at_24_khz(polza: FakePolza) -> None:
    tts = PolzaTts(url=polza.url)

    pcm = speak(tts, "Hello there.", "nova")

    assert tts.rate == RATE
    assert fake.decode(pcm, RATE) == fake.Decoded("Hello there.", frozenset({REGISTER}))
    [request] = polza.requests
    assert request.path == "/api/v1/audio/speech"
    assert request.authorization == f"Bearer {KEY}"
    assert request.body == {
        "model": "openai/tts-1",
        "input": "Hello there.",
        "voice": "nova",
        "response_format": "wav",
    }


def test_the_tts_asks_for_its_model_with_its_options(polza: FakePolza) -> None:
    tts = PolzaTts(url=polza.url, model="openai/gpt-4o-mini-tts", options={"speed": 1.25})

    speak(tts, "Hi.")

    assert polza.requests[0].body == {
        "model": "openai/gpt-4o-mini-tts",
        "input": "Hi.",
        "voice": "nova",
        "response_format": "wav",
        "speed": 1.25,
    }


def test_the_tts_fails_on_audio_that_isnt_a_wav_at_its_rate(polza: FakePolza) -> None:
    polza.content_type = "audio/mpeg"
    with pytest.raises(PolzaError, match=re.escape("polza.ai sent audio/mpeg, not audio/wav")):
        speak(PolzaTts(url=polza.url), "Hi.")

    polza.content_type, polza.rate = "audio/wav", 22050
    with pytest.raises(
        PolzaError, match=re.escape("polza.ai sent 22050 Hz audio: set the engine's rate")
    ):
        speak(PolzaTts(url=polza.url), "Hi.")

    assert fake.decode(speak(PolzaTts(url=polza.url, rate=22050), "Hi."), 22050).text == "Hi."


def test_the_stt_sends_the_audio_as_a_wav_and_hears_its_language_by_its_script(
    polza: FakePolza,
) -> None:
    stt = PolzaStt(url=polza.url)
    said = fake.encode("Привет.")

    heard = stt.transcribe(said, None)

    assert heard == Transcript("Привет.", "ru")
    [request] = polza.requests
    assert request.path == "/api/v1/audio/transcriptions"
    assert request.authorization == f"Bearer {KEY}"
    assert request.body.keys() == {"model", "file"}
    assert request.body["model"] == "openai/gpt-4o-mini-transcribe"
    assert pcm_of(request.body["file"]) == (said, 16000)


def test_the_stt_passes_on_the_language_asked_for(polza: FakePolza) -> None:
    stt = PolzaStt(url=polza.url, model="nvidia/parakeet-tdt-0.6b-v3")

    heard = stt.transcribe(fake.encode("Привет."), "en")

    assert heard == Transcript("Привет.", "en")
    assert polza.requests[0].body["language"] == "en"
    assert polza.requests[0].body["model"] == "nvidia/parakeet-tdt-0.6b-v3"


def test_an_error_from_polza_fails_the_call_with_its_message(polza: FakePolza) -> None:
    polza.status = 503

    failing = "polza.ai: 503 SERVICE_UNAVAILABLE: Все провайдеры вернули ошибку"  # noqa: RUF001
    with pytest.raises(PolzaError, match=re.escape(failing)):
        speak(PolzaTts(url=polza.url), "Hi.")
    with pytest.raises(PolzaError, match=re.escape("polza.ai: 503 SERVICE_UNAVAILABLE")):
        PolzaStt(url=polza.url).transcribe(fake.encode("Hi."), None)


def test_a_wrong_key_fails_the_call(polza: FakePolza, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLZA_API_KEY", "not-the-key")

    with pytest.raises(PolzaError, match=re.escape("polza.ai: 401 UNAUTHORIZED")):
        speak(PolzaTts(url=polza.url), "Hi.")


def test_the_key_comes_from_the_env_var_named(
    polza: FakePolza, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POLZA_API_KEY")
    monkeypatch.setenv("MY_POLZA", KEY)

    speak(PolzaTts(url=polza.url, key_env="MY_POLZA"), "Hi.")

    with pytest.raises(
        PolzaError, match=re.escape("polza.ai needs its API key in env POLZA_API_KEY")
    ):
        PolzaTts(url=polza.url)
    with pytest.raises(
        PolzaError, match=re.escape("polza.ai needs its API key in env POLZA_API_KEY")
    ):
        PolzaStt(url=polza.url)


def test_a_call_that_takes_longer_than_its_timeout_fails(polza: FakePolza) -> None:
    polza.delay = 1.0
    tts = PolzaTts(url=polza.url, timeout=0.2)

    started = time.monotonic()
    with pytest.raises(PolzaError, match=re.escape("polza.ai: timed out")):
        speak(tts, "Hi.")

    assert time.monotonic() - started < 0.8


def test_the_fake_sends_its_wav_as_polza_does() -> None:
    """With every size 0xFFFFFFFF, as polza.ai's own replies have it."""
    audio = wav(b"\x01\x00", 24000)

    assert base64.b64encode(audio[:8]) == base64.b64encode(b"RIFF\xff\xff\xff\xff")
    assert audio[36:44] == b"data\xff\xff\xff\xff"
