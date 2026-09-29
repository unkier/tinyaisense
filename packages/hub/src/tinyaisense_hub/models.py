"""The models the local speech engines run, pinned by URL and sha256, and `models fetch`,
which downloads them into the XDG data dir."""

import hashlib
import os
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .config import data_dir

_CHUNK = 1 << 20


class ModelError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ModelFile:
    name: str
    """Its path within its model's dir."""
    url: str
    """Where to download it from: a URL that pins its content."""
    sha256: str


@dataclass(frozen=True, slots=True)
class Model:
    name: str
    """Its dir within the models dir: named after its version, so that a new version gets a
    new dir."""
    files: tuple[ModelFile, ...]


def _hugging_face(repo: str, commit: str, name: str, sha256: str) -> ModelFile:
    return ModelFile(name, f"https://huggingface.co/{repo}/resolve/{commit}/{name}", sha256)


SILERO_VAD = Model(
    "silero-vad-v6.2.3",
    (
        ModelFile(
            "silero_vad.onnx",
            "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.3/src/silero_vad/data/"
            "silero_vad.onnx",
            "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
        ),
    ),
)
SMART_TURN = Model(
    "smart-turn-v3.2",
    (
        _hugging_face(
            "pipecat-ai/smart-turn-v3",
            "f766f81d3cfdf7737ac64aad813d91bbfd56bf93",
            "smart-turn-v3.2-cpu.onnx",
            "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f",
        ),
    ),
)


def _parakeet(name: str, sha256: str) -> ModelFile:
    return _hugging_face(
        "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
        "2bda32ec70b097a55adaa07d9a7173915b43cc78",
        name,
        sha256,
    )


PARAKEET = Model(
    "parakeet-tdt-0.6b-v3-int8",
    (
        _parakeet(
            "encoder.int8.onnx", "acfc2b4456377e15d04f0243af540b7fe7c992f8d898d751cf134c3a55fd2247"
        ),
        _parakeet(
            "decoder.int8.onnx", "179e50c43d1a9de79c8a24149a2f9bac6eb5981823f2a2ed88d655b24248db4e"
        ),
        _parakeet(
            "joiner.int8.onnx", "3164c13fc2821009440d20fcb5fdc78bff28b4db2f8d0f0b329101719c0948b3"
        ),
        _parakeet("tokens.txt", "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d"),
    ),
)
MODELS = (SILERO_VAD, SMART_TURN, PARAKEET)
"""Every model `models fetch` downloads."""


def models_dir() -> Path:
    return data_dir() / "models"


def model_dir(model: Model, path: str | None = None) -> Path:
    """Where `model` is: at `path` if given, else where `models fetch` puts it. Raises
    `ModelError` if any of its files isn't there."""
    where = models_dir() / model.name if path is None else Path(path)
    if missing := [file.name for file in model.files if not (where / file.name).is_file()]:
        raise ModelError(
            f"no model at {where} ({', '.join(missing)} missing): run "
            "`tinyaisense-hub models fetch`"
        )
    return where


def fetch(
    models: Iterable[Model], into: Path, report: Callable[[str], None] = lambda _: None
) -> None:
    """Download each model's files into a dir of its own in `into`, unless they're there
    already, checking each against its sha256. `report` hears how it goes. Raises `ModelError` or
    `OSError`."""
    for model in models:
        for file in model.files:
            path = into / model.name / file.name
            if path.exists() and _sha256(path) == file.sha256:
                report(f"{model.name}/{file.name}: already fetched")
                continue
            report(f"{model.name}/{file.name}: fetching {file.url}")
            _download(model, file, path)


def _download(model: Model, file: ModelFile, path: Path) -> None:
    """Download `file` to `path` by way of a `.part` file, so that `path` is only ever the
    whole file, checked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(file.url, timeout=30) as response, part.open("wb") as f:
            while chunk := response.read(_CHUNK):
                digest.update(chunk)
                f.write(chunk)
        if digest.hexdigest() != file.sha256:
            raise ModelError(
                f"{model.name}/{file.name} from {file.url} has sha256 {digest.hexdigest()}, "
                f"not the pinned {file.sha256}"
            )
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, path)


def _sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()
