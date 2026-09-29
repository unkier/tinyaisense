# The part of onnxruntime the hub uses, which ships no types.

from collections.abc import Mapping, Sequence
from os import PathLike
from typing import Any

import numpy as np

class SessionOptions:
    intra_op_num_threads: int
    inter_op_num_threads: int

class InferenceSession:
    def __init__(
        self,
        path_or_bytes: str | bytes | PathLike[str],
        sess_options: SessionOptions | None = None,
        providers: Sequence[str] | None = None,
    ) -> None: ...
    def run(
        self, output_names: Sequence[str] | None, input_feed: Mapping[str, np.ndarray[Any, Any]]
    ) -> list[np.ndarray[Any, Any]]: ...
