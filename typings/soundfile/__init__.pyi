# The part of soundfile the hub's bench and its tests use, which ships no types.

from os import PathLike
from typing import BinaryIO, Literal

import numpy as np

type _File = str | PathLike[str] | BinaryIO

def read(
    file: _File,
    frames: int = -1,
    start: int = 0,
    stop: int | None = None,
    dtype: Literal["float64", "float32", "int32", "int16"] = "float64",
    always_2d: bool = False,
) -> tuple[np.ndarray, int]: ...
def write(
    file: _File,
    data: np.ndarray,
    samplerate: int,
    subtype: str | None = None,
    endian: str | None = None,
    format: str | None = None,
) -> None: ...
