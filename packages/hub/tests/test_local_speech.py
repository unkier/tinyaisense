"""The local speech engines, on real speech: through the engine interface, and in `listen`.

They need the `[local]` extra and the models, which `tinyaisense-hub models fetch` downloads;
without them, these tests are skipped.
"""

import difflib
import wave
from pathlib import Path

import numpy as np
import pytest
import tinyaisense
from tinyaisense_hub import Hub, OwnerClient
from tinyaisense_hub.config import LOCAL_SPEECH
from tinyaisense_hub.models import MODELS, ModelError, model_dir
from tinyaisense_sim import Simulator, sources

from .conftest import StartHub
from .test_listen import paired

pytest.importorskip("sherpa_onnx", reason="the local engines need the [local] extra")


def _missing() -> str:
    """Why the models can't be used, if they can't."""
    try:
        for model in MODELS:
            model_dir(model)
    except ModelError as e:
        return str(e)
    return ""


MISSING = _missing()
pytestmark = pytest.mark.skipif(bool(MISSING), reason=MISSING)

from tinyaisense_hub.speech.local import ParakeetStt, SileroVad, SmartTurn  # noqa: E402

AUDIO = Path(__file__).parent / "audio"
RATE = 16000
EN = "ask not what your country can do for you ask what you can do for your country"
RU = (
    "ничьих не требуя похвал счастлив уж я надеждой сладкой что дева с трепетом любви "  # noqa: RUF001
    "посмотрит может быть украдкой на песни грешные мои"
)


def speech(name: str) -> bytes:
    """A sample of human speech from `audio/`, 16 kHz s16le."""
    with wave.open(str(AUDIO / name)) as f:
        assert (f.getframerate(), f.getnchannels(), f.getsampwidth()) == (RATE, 1, 2)
        return f.readframes(f.getnframes())


def silence(seconds: float) -> bytes:
    return bytes(2 * int(seconds * RATE))


def heard(transcript: str, said: str) -> bool:
    """Whether `transcript` is close enough to what was `said`, in lower case and without
    punctuation: the STT may slip on a letter or a space."""
    words = " ".join("".join(c for c in word if c.isalpha()) for word in transcript.split())
    return difflib.SequenceMatcher(None, words.lower(), said).ratio() >= 0.9


@pytest.fixture(scope="module")
def vad() -> SileroVad:
    vad = SileroVad()
    vad.warm_up()
    return vad


def ratings(vad: SileroVad, state: object, pcm: bytes) -> list[float]:
    """The VAD's rating of each whole window of `pcm`, on one stream."""
    size = vad.window * 2
    return [
        vad.probability(state, pcm[at : at + size]) for at in range(0, len(pcm) - size + 1, size)
    ]


def test_silero_hears_speech_as_speech_and_silence_as_silence(vad: SileroVad) -> None:
    heard = ratings(vad, vad.new_state(), speech("en.wav"))
    quiet = ratings(vad, vad.new_state(), silence(1))

    assert np.mean(np.array(heard) >= 0.5) > 0.7
    assert max(quiet) < 0.35


def test_silero_keeps_each_streams_state_apart(vad: SileroVad) -> None:
    alone = ratings(vad, vad.new_state(), speech("en.wav"))
    english, russian = vad.new_state(), vad.new_state()
    size = vad.window * 2
    en, ru = speech("en.wav"), speech("ru.wav")
    interleaved: list[float] = []
    for at in range(0, len(en) - size + 1, size):
        interleaved.append(vad.probability(english, en[at : at + size]))
        vad.probability(russian, ru[at : at + size])

    assert interleaved == alone


@pytest.fixture(scope="module")
def turn() -> SmartTurn:
    turn = SmartTurn()
    turn.warm_up()
    return turn


def test_smart_turn_hears_a_finished_sentence_as_a_complete_turn(turn: SmartTurn) -> None:
    assert turn.complete(silence(0.3) + speech("en.wav")) >= 0.5


def test_smart_turn_hears_a_sentence_cut_short_as_an_incomplete_turn(turn: SmartTurn) -> None:
    # "Ask not what your country can do for you, ask what you"
    assert turn.complete(silence(0.3) + speech("en.wav")[: 2 * 3 * RATE]) < 0.5


@pytest.fixture(scope="module")
def stt() -> ParakeetStt:
    stt = ParakeetStt()
    stt.warm_up()
    return stt


SAMPLES = pytest.mark.parametrize(
    ("sample", "said", "lang"), [("en.wav", EN, "en"), ("ru.wav", RU, "ru")], ids=["en", "ru"]
)


@SAMPLES
def test_parakeet_transcribes_speech_in_the_language_it_hears(
    stt: ParakeetStt, sample: str, said: str, lang: str
) -> None:
    transcript = stt.transcribe(speech(sample), None)

    assert heard(transcript.text, said)
    assert transcript.lang == lang


@pytest.fixture
async def hub(start_hub: StartHub) -> Hub:
    """With the local engines that `init` configures."""
    return await start_hub(speech=LOCAL_SPEECH)


@SAMPLES
async def test_listen_hears_what_the_human_says_in_their_language(
    hub: Hub, sim: Simulator, owner: OwnerClient, sample: str, said: str, lang: str
) -> None:
    kitchen = await paired(sim, owner)
    kitchen.mic.feed(sources.silence(300) + sources.wav(AUDIO / sample))
    async with tinyaisense.connect(hub.agent_url, "agent-token") as agent:
        result = await agent.listen("kitchen/mic")

    assert (result["reason"], result["lang"]) == ("end_of_turn", lang)
    assert heard(result["transcript"], said)
