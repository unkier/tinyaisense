"""The hub's resampler: a streaming polyphase FIR between 16, 24 and 48 kHz. Sans-IO.

Every rate divides 48 kHz, so each conversion is one low-pass at 48 kHz, split into phases: a
Kaiser-windowed sinc. Of the lower Nyquist, it's flat to 0.8, and ~86 dB down from 1.125, so
nothing folds back or images below 0.875: 7 kHz at 16 kHz. The filter is causal and
linear-phase, so the audio comes out 1 ms late, and whatever comes in comes out at once: 10 ms
in gives 10 ms out.
"""

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

RATES = (16000, 24000, 48000)
"""The rates it converts between: an agent streams at any of them."""
_FILTER_RATE = 48000
"""The rate every conversion filters at."""
_TAPS = 97
"""The filter's length at 48 kHz: it delays by half of it less one, 48 samples or 1 ms."""
_BETA = 8.6
"""The Kaiser window's β."""
_CUTOFF = 0.95
"""Where the low-pass is 6 dB down, as a fraction of the lower Nyquist."""


class Resampler:
    """s16le mono PCM from rate `source` to `target`, both of them 16, 24 or 48 kHz, any
    number of samples at a time."""

    def __init__(self, source: int, target: int) -> None:
        self._source = source
        self._up = _FILTER_RATE // source
        self._down = _FILTER_RATE // target
        self._width = -(-_TAPS // self._up)
        """How many input samples each output sample weighs."""
        taps = np.zeros(self._width * self._up)
        taps[:_TAPS] = _lowpass(_CUTOFF * min(source, target) / 2) * self._up
        self._phases = taps.reshape(self._width, self._up).T[:, ::-1]
        """Row p weighs the `width` input samples up to an output's, oldest first, for an
        output p samples at 48 kHz past that input sample."""
        self._history = np.zeros(self._width - 1)
        """The last input samples, which the next outputs still weigh."""
        self._taken = 0
        """Input samples taken so far."""
        self._next = 0
        """The next output sample's position at 48 kHz, from the first input sample."""

    def process(self, pcm: bytes) -> bytes:
        """Take input, and return every output sample it completes."""
        start = self._taken - len(self._history)  # where `buffer[0]` sits in the input
        buffer = np.concatenate((self._history, np.frombuffer(pcm, "<i2")))
        self._taken += len(pcm) // 2
        self._history = buffer[len(buffer) - len(self._history) :]
        first = self._next
        out = np.empty(len(range(first, self._taken * self._up, self._down)))
        if not len(out):
            return b""
        self._next += len(out) * self._down
        windows = sliding_window_view(buffer, self._width)
        # Outputs `up` apart share a phase, and their windows sit `down` input samples apart.
        for k in range(min(self._up, len(out))):
            last, phase = divmod(first + k * self._down, self._up)
            rows = windows[last - (self._width - 1) - start :: self._down]
            out[k :: self._up] = rows[: len(out[k :: self._up])] @ self._phases[phase]
        return np.clip(np.rint(out), -32768, 32767).astype("<i2").tobytes()

    def flush(self) -> bytes:
        """The last 1 ms of the input, which the filter still holds: no more input follows."""
        return self.process(bytes(self._source // 1000 * 2))


def _lowpass(cutoff: float) -> np.ndarray:
    """A Kaiser-windowed sinc at 48 kHz, with unity gain at DC."""
    n = np.arange(_TAPS) - (_TAPS - 1) / 2
    taps = np.sinc(2 * cutoff / _FILTER_RATE * n) * np.kaiser(_TAPS, _BETA)
    return taps / taps.sum()
