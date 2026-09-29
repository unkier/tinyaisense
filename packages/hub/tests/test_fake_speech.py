"""The fake speech plugin: what the speech tests speak and hear through."""

from tinyaisense_hub.speech import Transcript
from tinyaisense_hub.speech.fake import FakeStt, FakeTts


def spoken(*texts: str) -> bytes:
    """`texts` as the fake TTS says them, with 50 ms of silence after each."""
    tts = FakeTts()
    return b"".join(b"".join(tts.synthesize(text, "any")) + bytes(1600) for text in texts)


def test_the_fake_stt_hears_what_the_fake_tts_said_less_the_silences() -> None:
    assert FakeStt().transcribe(spoken("Hello", " there."), None) == Transcript(
        "Hello there.", "en"
    )


def test_the_fake_stt_hears_the_language_of_the_text_unless_it_is_told() -> None:
    stt = FakeStt()

    assert stt.transcribe(spoken("Привет, мир"), None).lang == "ru"
    assert stt.transcribe(spoken("Привет, мир"), "en").lang == "en"
    assert stt.transcribe(spoken(), None) == Transcript("", "en")  # its first language
