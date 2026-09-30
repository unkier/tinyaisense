"""The speech bench's corpus: clips pinned by sha256 within archives, fetched once, and read as
the device's audio, cut to their speech."""

import hashlib
import io
import tarfile
from pathlib import Path

import numpy as np
import pytest
import soundfile
from tinyaisense_hub.bench.corpus import Archive, BenchError, Clip, fetch, speech


def archive(tmp_path: Path, files: dict[str, bytes], name: str = "speech.tar.gz") -> Archive:
    """An archive of `files`, served from the test's own dir."""
    path = tmp_path / "source" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for member, data in files.items():
            info = tarfile.TarInfo(member)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return Archive("speech-v1", path.as_uri())


def clip(source: Archive, member: str, data: bytes, speech: tuple[int, int] = (0, 0)) -> Clip:
    return Clip(source, member, hashlib.sha256(data).hexdigest(), "en", "hello", speech)


def test_fetch_takes_only_the_pinned_clips_out_of_their_archive(tmp_path: Path) -> None:
    source = archive(tmp_path, {"a/one.flac": b"one", "a/two.flac": b"two", "b/three.flac": b"3"})

    fetch([clip(source, "a/one.flac", b"one"), clip(source, "b/three.flac", b"3")], tmp_path / "c")

    assert sorted(p.name for p in (tmp_path / "c" / "speech-v1").iterdir()) == [
        "one.flac",
        "three.flac",
    ]
    assert (tmp_path / "c" / "speech-v1" / "one.flac").read_bytes() == b"one"


def test_a_clip_that_isnt_what_was_pinned_fails_the_fetch_and_isnt_kept(tmp_path: Path) -> None:
    source = archive(tmp_path, {"a/one.flac": b"something else"})

    with pytest.raises(BenchError, match=r"one\.flac.*sha256"):
        fetch([clip(source, "a/one.flac", b"one")], tmp_path / "c")

    assert list((tmp_path / "c").rglob("*.*")) == []


def test_a_clip_missing_from_its_archive_fails_the_fetch(tmp_path: Path) -> None:
    source = archive(tmp_path, {"a/one.flac": b"one"})

    with pytest.raises(BenchError, match=r"a/two\.flac"):
        fetch(
            [clip(source, "a/one.flac", b"one"), clip(source, "a/two.flac", b"2")], tmp_path / "c"
        )


def test_clips_already_fetched_arent_fetched_again(tmp_path: Path) -> None:
    source = archive(tmp_path, {"a/one.flac": b"one"})
    fetch([clip(source, "a/one.flac", b"one")], tmp_path / "c")
    (tmp_path / "source" / "speech.tar.gz").unlink()

    fetch([clip(source, "a/one.flac", b"one")], tmp_path / "c")

    assert (tmp_path / "c" / "speech-v1" / "one.flac").read_bytes() == b"one"


def test_speech_is_the_clip_at_16_khz_from_300_ms_before_its_speech_to_its_end(
    tmp_path: Path,
) -> None:
    ramp = np.linspace(-0.5, 0.5, 48000, dtype=np.float32)  # 1 s at 48 kHz
    flac = io.BytesIO()
    soundfile.write(flac, ramp, 48000, format="FLAC")
    data = flac.getvalue()
    source = archive(tmp_path, {"a/ramp.flac": data})
    ramped = clip(source, "a/ramp.flac", data, speech=(500, 900))
    fetch([ramped], tmp_path / "c")

    pcm = np.frombuffer(speech(ramped, tmp_path / "c"), "<i2")

    assert len(pcm) == 16 * (900 - 200)
    at = np.linspace(-0.5, 0.5, 16000)[16 * 200 : 16 * 900] * 32767
    assert np.abs(pcm - at).max() < 100


def test_speech_starting_early_in_its_clip_keeps_what_there_is_before_it(tmp_path: Path) -> None:
    silence = io.BytesIO()
    soundfile.write(silence, np.zeros(16000, np.float32), 16000, format="FLAC")
    data = silence.getvalue()
    source = archive(tmp_path, {"a/early.flac": data})
    early = clip(source, "a/early.flac", data, speech=(100, 600))
    fetch([early], tmp_path / "c")

    assert len(speech(early, tmp_path / "c")) == 2 * 16 * 600
