# The part of soxr the hub and its tests use, which ships no types.

from typing import Literal

import numpy as np

type Quality = Literal["QQ", "LQ", "MQ", "HQ", "VHQ"]

class ResampleStream:
    def __init__(
        self,
        in_rate: float,
        out_rate: float,
        num_channels: int,
        dtype: str = "float32",
        quality: Quality = "HQ",
    ) -> None: ...
    def resample_chunk[A: np.ndarray](self, x: A, last: bool = False) -> A: ...

def resample[A: np.ndarray](
    x: A, in_rate: float, out_rate: float, quality: Quality = "HQ"
) -> A: ...
