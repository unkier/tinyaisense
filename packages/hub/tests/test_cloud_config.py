"""A cloud speech engine in the hub config: named for a language along with the local engine
that stands in when it fails."""

import pytest
from tinyaisense_hub import ConfigError, Timing
from tinyaisense_hub.config import parse_config, render_config

CONFIG = """
agent_token = "token"

[speech]
vad = "vad"

[speech.tts]
en = "local_tts"
ru = ["cloud_tts", "local_tts"]

[speech.stt]
en = ["cloud_stt", "local_stt"]
ru = "local_stt"

[speech.engines.vad]
class = "tinyaisense_hub.speech.fake:FakeVad"

[speech.engines.local_tts]
class = "tinyaisense_hub.speech.fake:FakeTts"

[speech.engines.cloud_tts]
class = "tinyaisense_hub.speech.polza:PolzaTts"

[speech.engines.local_stt]
class = "tinyaisense_hub.speech.fake:FakeStt"

[speech.engines.cloud_stt]
class = "tinyaisense_hub.speech.polza:PolzaStt"

[[speech.voices]]
id = "alto"
lang = "en"
name = "Alto"

[[speech.voices]]
id = "nova"
lang = "ru"
name = "Nova"
fallback_voice = "masha"
"""


def test_a_language_takes_a_cloud_engine_and_the_local_one_that_stands_in_for_it() -> None:
    speech = parse_config(CONFIG).speech

    assert speech.tts == {"en": "local_tts", "ru": ("cloud_tts", "local_tts")}
    assert speech.stt == {"en": ("cloud_stt", "local_stt"), "ru": "local_stt"}


def test_a_voice_names_the_stand_ins_voice_which_is_its_own_unless_given() -> None:
    alto, nova = parse_config(CONFIG).speech.voices

    assert (alto.engine_voice, alto.fallback_voice) == ("alto", "alto")
    assert (nova.engine_voice, nova.fallback_voice) == ("nova", "masha")


def test_the_cloud_waits_1_s_for_a_first_byte_and_is_skipped_for_60_s_by_default() -> None:
    timing = parse_config(CONFIG).timing

    assert (timing.cloud_first_byte_ms, timing.cloud_skip_ms) == (1000, 60000)
    assert timing == Timing()


def test_a_config_with_cloud_engines_renders_as_it_parses() -> None:
    config = parse_config(CONFIG)

    assert parse_config(render_config(config)) == config


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (
            ('ru = ["cloud_tts", "local_tts"]', 'ru = ["cloud_tts"]'),
            "speech.tts.ru must be an engine's name, or a cloud engine's and its stand-in's",
        ),
        (
            ('ru = ["cloud_tts", "local_tts"]', 'ru = ["cloud_tts", "local_tts", "x"]'),
            "speech.tts.ru must be an engine's name, or a cloud engine's and its stand-in's",
        ),
        (
            ('en = ["cloud_stt", "local_stt"]', 'en = ["cloud_stt", 7]'),
            "speech.stt.en must be an engine's name, or a cloud engine's and its stand-in's",
        ),
        (
            ('ru = ["cloud_tts", "local_tts"]', 'ru = ["cloud_tts", "cloud_tts"]'),
            "speech.tts.ru: engine 'cloud_tts' can't stand in for itself",
        ),
        (
            ('ru = ["cloud_tts", "local_tts"]', 'ru = ["cloud_tts", "piper"]'),
            "speech.tts.ru: no engine 'piper' in [speech.engines]",
        ),
        (
            ('en = ["cloud_stt", "local_stt"]', 'en = ["whisper", "local_stt"]'),
            "speech.stt.en: no engine 'whisper' in [speech.engines]",
        ),
    ],
)
def test_a_cloud_engine_and_its_stand_in_must_be_two_engines_of_the_config(
    change: tuple[str, str], error: str
) -> None:
    with pytest.raises(ConfigError) as refused:
        parse_config(CONFIG.replace(*change))

    assert str(refused.value) == error
