"""The local speech engines, which run on the hub's CPU: Silero VAD, Smart Turn and Parakeet.
They need the `[local]` extra, and their models, which `models fetch` downloads."""

from dataclasses import dataclass

import numpy as np

try:
    import onnxruntime as ort
    import sherpa_onnx
except ImportError as e:
    raise ImportError(f"{e}: the local speech engines need tinyaisense-hub[local]") from e

from ..models import PARAKEET, SILERO_VAD, SMART_TURN, model_dir
from .engines import Stt, Transcript, Turn, Vad
from .text import language


def _onnx(path: str, threads: int) -> ort.InferenceSession:
    """An ONNX model on the CPU, running each call on `threads` threads."""
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    return ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])


_SCALE = 1 / 32768
"""From s16 samples to floats in -1..1."""


def _floats(pcm: bytes) -> np.ndarray[tuple[int], np.dtype[np.float32]]:
    return np.frombuffer(pcm, "<i2").astype(np.float32) * np.float32(_SCALE)


@dataclass(slots=True)
class _SileroStream:
    """What Silero carries from one window of a stream to the next."""

    state: np.ndarray[tuple[int, int, int], np.dtype[np.float32]]
    context: np.ndarray[tuple[int], np.dtype[np.float32]]
    """The end of the previous window, which the model hears again before the next."""


class SileroVad(Vad):
    """Silero VAD v6, on ONNX Runtime: one model for every stream, each with its own state."""

    rate = 16000
    window = 512

    _CONTEXT = 64
    """Samples of the previous window that the model hears before each window, at 16 kHz."""

    def __init__(self, *, threads: int = 1, model: str | None = None) -> None:
        """`model` is the dir of the model, if it isn't where `models fetch` puts it."""
        path = model_dir(SILERO_VAD, model) / "silero_vad.onnx"
        self._model = _onnx(str(path), threads)
        self._rate = np.array(self.rate, np.int64)

    def warm_up(self) -> None:
        self.probability(self.new_state(), bytes(2 * self.window))

    def new_state(self) -> object:
        return _SileroStream(np.zeros((2, 1, 128), np.float32), np.zeros(self._CONTEXT, np.float32))

    def probability(self, state: object, pcm: bytes) -> float:
        assert isinstance(state, _SileroStream)
        heard = np.concatenate((state.context, _floats(pcm)))
        p, state.state = self._model.run(
            None, {"input": heard[np.newaxis], "state": state.state, "sr": self._rate}
        )
        state.context = heard[-self._CONTEXT :]
        return float(p[0, 0])


class SmartTurn(Turn):
    """Smart Turn v3.2, on ONNX Runtime: whether the last 8 s of audio end the turn."""

    rate = 16000

    _SECONDS = 8
    """How much of the latest audio it hears."""

    def __init__(self, *, threads: int = 4, model: str | None = None) -> None:
        """`model` is the dir of the model, if it isn't where `models fetch` puts it."""
        path = model_dir(SMART_TURN, model) / "smart-turn-v3.2-cpu.onnx"
        self._model = _onnx(str(path), threads)

    def warm_up(self) -> None:
        self.complete(bytes(2 * self.rate))

    def complete(self, pcm: bytes) -> float:
        """Of the last 8 s: shorter audio is padded with silence before it."""
        samples = self._SECONDS * self.rate
        audio = _floats(pcm)[-samples:]
        audio = np.pad(audio, (samples - len(audio), 0))
        (p,) = self._model.run(None, {"input_features": _log_mel(audio)[np.newaxis]})
        return float(p[0, 0])


class ParakeetStt(Stt):
    """Parakeet TDT 0.6B v3, int8, on sherpa-onnx. It hears English and Russian, and tells
    which by itself: it takes no `lang`, and the language of its transcript is that of the
    script it's written in."""

    languages = ("en", "ru")
    rate = 16000

    def __init__(self, *, threads: int = 4, model: str | None = None) -> None:
        """`model` is the dir of the model, if it isn't where `models fetch` puts it."""
        where = model_dir(PARAKEET, model)
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            str(where / "encoder.int8.onnx"),
            str(where / "decoder.int8.onnx"),
            str(where / "joiner.int8.onnx"),
            str(where / "tokens.txt"),
            threads,
            model_type="nemo_transducer",
        )

    def warm_up(self) -> None:
        self.transcribe(bytes(2 * self.rate), None)

    def transcribe(self, pcm: bytes, lang: str | None) -> Transcript:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(self.rate, _floats(pcm))
        self._recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        return Transcript(text, language(text) or lang or self.languages[0])


_N_FFT = 400
"""Samples per spectrum: 25 ms."""
_HOP = 160
"""Samples from one spectrum to the next: 10 ms."""
_MELS = 80
"""Mel bands per frame."""


def _log_mel(audio: np.ndarray[tuple[int], np.dtype[np.float32]]) -> np.ndarray:
    """Whisper's log-mel features of 16 kHz `audio`, as its feature extractor in Hugging Face
    transformers makes them with `do_normalize`: `(80, frames)`, float32."""
    x = audio.astype(np.float64)
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    x = np.pad(x, _N_FFT // 2, mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(x, _N_FFT)[::_HOP]
    power = np.abs(np.fft.rfft(frames * _HANN, axis=1)) ** 2
    log = np.log10(np.maximum(power @ _MEL_FILTERS, 1e-10)).T[:, :-1]
    log = np.maximum(log, log.max() - 8.0)
    return ((log + 4.0) / 4.0).astype(np.float32)


def _mel_filters() -> np.ndarray:
    """Whisper's mel filter bank, `(bins, mels)`: triangles evenly spaced on the Slaney mel
    scale from 0 to 8 kHz, each normalized to the same area."""

    def mel(hz: np.ndarray) -> np.ndarray:
        # Linear below 1 kHz, logarithmic above.
        log = 15.0 + np.log(np.maximum(hz, 1e-10) / 1000.0) * 27.0 / np.log(6.4)
        return np.where(hz >= 1000.0, log, hz * 3.0 / 200.0)

    def hz(mel: np.ndarray) -> np.ndarray:
        return np.where(
            mel >= 15.0, 1000.0 * np.exp((mel - 15.0) * np.log(6.4) / 27.0), mel * 200.0 / 3.0
        )

    bins = np.linspace(0.0, 8000.0, 1 + _N_FFT // 2)
    edges = hz(np.linspace(mel(np.array(0.0)), mel(np.array(8000.0)), _MELS + 2))
    widths = np.diff(edges)
    offsets = edges[np.newaxis, :] - bins[:, np.newaxis]
    rising = -offsets[:, :-2] / widths[:-1]
    falling = offsets[:, 2:] / widths[1:]
    triangles = np.maximum(0.0, np.minimum(rising, falling))
    return triangles * (2.0 / (edges[2:] - edges[:-2]))


_HANN = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(_N_FFT) / _N_FFT)
"""The periodic Hann window each spectrum is taken through."""
_MEL_FILTERS = _mel_filters()
