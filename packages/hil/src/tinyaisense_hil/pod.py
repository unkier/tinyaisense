"""The acoustic rig's audio, through PortAudio: the POD recording all it hears, with the ADC time
of each block, and the PC speaker playing into the Echo's mic as a mouth. Importing it loads
PortAudio."""

import collections
import threading
import time
from types import TracebackType
from typing import Any, Self, cast

import numpy as np
import sounddevice as sd

from .acoustic import Heard, Timeline

RATE = 48000
_CLOCK_SLACK_S = 1.0
"""How far PortAudio's clock may be from CLOCK_MONOTONIC and still be it: ALSA's hardware
devices use it, PipeWire's and JACK's don't."""
_FIRST_BLOCK_S = 2.0
_LATENCY_S = 0.1
"""PortAudio's buffering: enough that a busy test, holding the GIL, doesn't make the POD's
recording overflow, or the mouth's chirps underflow. The ADC times don't depend on it."""


class PodError(Exception):
    pass


def _channels(device: str | None, kind: str) -> int:
    """How many channels to open `device` with: an ALSA hardware device takes only its own."""
    try:
        info = cast(dict[str, Any], sd.query_devices(device, kind))
    except (ValueError, sd.PortAudioError) as e:
        raise PodError(f"no {kind} device {device!r} in PortAudio: {e}") from None
    return min(2, int(info[f"max_{kind}_channels"]))


class Pod:
    """The POD, the mic touching the Echo, recording its first channel from when it's entered
    until it's left: what it heard, and when, by the ADC time of each block. Raises
    `PodError`."""

    def __init__(self, device: str) -> None:
        self._device = device
        self._blocks: list[tuple[int, float]] = []
        """Each block's first sample, and its ADC time, in epoch ms."""
        self._audio: list[np.ndarray] = []
        self._samples = 0
        self._overflows = 0
        self._lock = threading.Lock()
        self._offset_ms = 0.0
        self._skew_s = 0.0
        """How far PortAudio's clock was from CLOCK_MONOTONIC at the first block."""
        self._started = threading.Event()
        try:
            self._stream = sd.InputStream(
                samplerate=RATE,
                device=device,
                channels=_channels(device, "input"),
                dtype="float32",
                latency=_LATENCY_S,
                callback=self._block,
            )
        except sd.PortAudioError as e:
            raise PodError(f"can't record the POD, {device!r}: {e}") from None

    def __enter__(self) -> Self:
        self._offset_ms = time.time_ns() / 1e6 - time.monotonic_ns() / 1e6
        self._stream.start()
        if not self._started.wait(_FIRST_BLOCK_S):
            self._stream.close()
            raise PodError(f"the POD, {self._device!r}, records nothing")
        if abs(self._skew_s) > _CLOCK_SLACK_S:
            self._stream.close()
            raise PodError(
                f"PortAudio's clock for the POD, {self._device!r}, isn't CLOCK_MONOTONIC: name "
                "its ALSA hardware device, hw:, in the rig config"
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stream.stop()
        self._stream.close()

    def heard(self, lag_ms: float = 0.0) -> Heard:
        """What the POD has heard so far, with its `lag_ms` taken off. Raises `PodError` if its
        recording has gaps."""
        with self._lock:
            blocks, audio, overflows = list(self._blocks), list(self._audio), self._overflows
        if overflows:
            raise PodError(f"the POD's recording overflowed {overflows} times: it has gaps")
        if len(blocks) < 2:
            raise PodError("the POD has heard nothing yet")
        return Heard(np.concatenate(audio), RATE, Timeline.fit(blocks), lag_ms)

    def _block(self, indata: Any, frames: int, times: Any, status: sd.CallbackFlags) -> None:
        block = cast(np.ndarray, indata)[:, 0].copy()
        adc_ms = cast(float, times.inputBufferAdcTime) * 1000 + self._offset_ms
        if not self._started.is_set():
            self._skew_s = cast(float, times.currentTime) - time.monotonic()
            self._started.set()
        with self._lock:
            if status.input_overflow:
                self._overflows += 1
            self._blocks.append((self._samples, adc_ms))
            self._audio.append(block)
            self._samples += frames


class Mouth:
    """The PC speaker by the Echo's mic, playing what it's given at once, after what it's
    playing; PortAudio's default output if `device` is None. Raises `PodError`."""

    def __init__(self, device: str | None) -> None:
        self._queue: collections.deque[np.ndarray] = collections.deque()
        self._playing = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        try:
            self._stream = sd.OutputStream(
                samplerate=RATE,
                device=device,
                channels=_channels(device, "output"),
                dtype="float32",
                latency=_LATENCY_S,
                callback=self._fill,
            )
        except sd.PortAudioError as e:
            raise PodError(f"can't play on the mouth, {device!r}: {e}") from None

    def __enter__(self) -> Self:
        self._stream.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stream.stop()
        self._stream.close()

    def play(self, signal: np.ndarray) -> None:
        """Play `signal`, at `RATE`, peaking at 1 or less."""
        with self._lock:
            self._queue.append(signal.astype(np.float32))

    def _fill(self, outdata: Any, frames: int, times: Any, status: sd.CallbackFlags) -> None:
        out = cast(np.ndarray, outdata)
        out.fill(0)
        filled = 0
        with self._lock:
            while filled < frames:
                if not len(self._playing):
                    if not self._queue:
                        return
                    self._playing = self._queue.popleft()
                take = min(frames - filled, len(self._playing))
                out[filled : filled + take] = self._playing[:take, None]
                self._playing = self._playing[take:]
                filled += take
