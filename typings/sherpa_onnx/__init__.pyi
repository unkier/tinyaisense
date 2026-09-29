# The part of sherpa-onnx the hub uses, which ships no types.

from typing import Any

import numpy as np

class OfflineRecognitionResult:
    @property
    def text(self) -> str: ...

class OfflineStream:
    def accept_waveform(self, sample_rate: int, waveform: np.ndarray[Any, Any]) -> None: ...
    @property
    def result(self) -> OfflineRecognitionResult: ...

class OfflineRecognizer:
    @classmethod
    def from_transducer(
        cls,
        encoder: str,
        decoder: str,
        joiner: str,
        tokens: str,
        num_threads: int = 1,
        *,
        model_type: str = "transducer",
    ) -> OfflineRecognizer: ...
    def create_stream(self) -> OfflineStream: ...
    def decode_stream(self, s: OfflineStream) -> None: ...
