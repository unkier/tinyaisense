"""`models fetch`: the pinned models, downloaded and checked by sha256."""

import hashlib
from pathlib import Path

import pytest
from tinyaisense_hub.models import Model, ModelError, ModelFile, fetch


def served(tmp_path: Path, name: str, content: bytes) -> ModelFile:
    """A file to fetch, served from the test's own dir, pinned to its content."""
    source = tmp_path / "source" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return ModelFile(name, source.as_uri(), hashlib.sha256(content).hexdigest())


def test_fetch_puts_each_models_files_in_a_dir_of_its_own(tmp_path: Path) -> None:
    vad = Model("vad-v1", (served(tmp_path, "vad.onnx", b"vad weights"),))
    stt = Model(
        "stt-v2",
        (served(tmp_path, "encoder.onnx", b"encoder"), served(tmp_path, "tokens.txt", b"a b c")),
    )

    fetch([vad, stt], tmp_path / "models")

    assert (tmp_path / "models" / "vad-v1" / "vad.onnx").read_bytes() == b"vad weights"
    assert (tmp_path / "models" / "stt-v2" / "encoder.onnx").read_bytes() == b"encoder"
    assert (tmp_path / "models" / "stt-v2" / "tokens.txt").read_bytes() == b"a b c"


def test_a_file_that_isnt_what_was_pinned_fails_the_fetch_and_isnt_kept(tmp_path: Path) -> None:
    pinned = served(tmp_path, "vad.onnx", b"vad weights")
    (tmp_path / "source" / "vad.onnx").write_bytes(b"something else")

    with pytest.raises(ModelError, match=r"vad-v1/vad\.onnx.*sha256"):
        fetch([Model("vad-v1", (pinned,))], tmp_path / "models")

    assert list((tmp_path / "models").rglob("*.*")) == []


def test_a_file_already_fetched_isnt_downloaded_again(tmp_path: Path) -> None:
    vad = Model("vad-v1", (served(tmp_path, "vad.onnx", b"vad weights"),))
    fetch([vad], tmp_path / "models")
    (tmp_path / "source" / "vad.onnx").unlink()

    fetch([vad], tmp_path / "models")

    assert (tmp_path / "models" / "vad-v1" / "vad.onnx").read_bytes() == b"vad weights"


def test_a_file_that_changed_since_it_was_fetched_is_downloaded_again(tmp_path: Path) -> None:
    vad = Model("vad-v1", (served(tmp_path, "vad.onnx", b"vad weights"),))
    fetch([vad], tmp_path / "models")
    (tmp_path / "models" / "vad-v1" / "vad.onnx").write_bytes(b"vad weigh")

    fetch([vad], tmp_path / "models")

    assert (tmp_path / "models" / "vad-v1" / "vad.onnx").read_bytes() == b"vad weights"
