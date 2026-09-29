"""Stream formats: docs/protocol/audio.md#format."""

from tinyaisense_wire import AudioFormat

PCM16 = {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": [10, 20]}


def test_a_format_is_offered_at_any_frame_ms_the_capability_lists() -> None:
    assert AudioFormat("pcm_s16le", 16000, 1, 20).offered({"formats": [PCM16]})


def test_a_format_is_not_offered_at_a_frame_ms_the_capability_doesnt_list() -> None:
    assert not AudioFormat("pcm_s16le", 16000, 1, 30).offered({"formats": [PCM16]})


def test_a_format_is_not_offered_at_another_rate_or_by_a_capability_without_formats() -> None:
    assert not AudioFormat("pcm_s16le", 48000, 1, 10).offered({"formats": [PCM16]})
    assert not AudioFormat("pcm_s16le", 16000, 1, 10).offered(None)


def test_a_format_round_trips_through_json() -> None:
    format = AudioFormat("pcm_s16le", 16000, 1, 10)

    assert format.to_json() == {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": 10}
    assert AudioFormat.from_json(format.to_json()) == format
