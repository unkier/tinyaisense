"""The PC's own audio, for a simulated device: its speaker as a sink, its mic as a source.

Needs the `[audio]` extra, and PortAudio.
"""

import asyncio
import threading
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Literal

from .mic import SimMic
from .sinks import CapturedFrame, scale
from .sources import RATE

if TYPE_CHECKING:
    from sounddevice import CallbackFlags, RawInputStream, RawOutputStream

type _Callback = Callable[[Any, int, Any, CallbackFlags], None]
"""PortAudio's: `(buffer, frames, time, status)`, on its own thread."""

BLOCK = RATE // 50
"""Samples PortAudio moves at a time: 20 ms."""
MAX_LAG_MS = 200
"""Audio queued for the PC's speaker, or fed to a mic and not yet heard, past this much is
dropped: the PC's clock and the device's drift apart."""
_MAX_LAG_BYTES = MAX_LAG_MS * RATE // 1000 * 2


class NoAudio(Exception):
    """The PC's audio can't be used here."""


def _sounddevice():  # the module, which pyright infers
    try:
        import sounddevice  # the [audio] extra is optional
    except ImportError as e:
        raise NoAudio(
            "the PC's audio needs the [audio] extra: pip install 'tinyaisense-sim[audio]'"
        ) from e
    except OSError as e:  # sounddevice is in, PortAudio isn't
        raise NoAudio(f"the PC's audio needs PortAudio: {e}") from e
    return sounddevice


def _open(way: Literal["output", "input"], callback: _Callback) -> RawOutputStream | RawInputStream:
    """Start a 16 kHz mono stream on the PC's default output or input."""
    sd = _sounddevice()
    stream = sd.RawOutputStream if way == "output" else sd.RawInputStream
    try:
        opened = stream(
            samplerate=RATE, blocksize=BLOCK, channels=1, dtype="int16", callback=callback
        )
        opened.start()
    except sd.PortAudioError as e:
        raise NoAudio(f"the PC's audio {way}: {e}") from e
    return opened


class PcSink:
    """Plays what a simulated speaker hears on the PC's default output, at the volume. Raises
    `NoAudio` if it can't."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queued = bytearray()
        """s16le audio for PortAudio to take, guarded by `_lock`."""
        self._stream = _open("output", self._pull)

    def play(self, frame: CapturedFrame, gain: float) -> None:
        with self._lock:
            self._queued.extend(scale(frame.pcm, gain))
            del self._queued[: max(0, len(self._queued) - _MAX_LAG_BYTES)]

    def cut(self, at: float) -> None:
        with self._lock:
            self._queued.clear()

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()

    def _pull(self, out: Any, frames: int, time: Any, status: Any) -> None:
        """PortAudio's thread wants `frames` samples: silence for what isn't there."""
        size = frames * 2
        with self._lock:
            pcm = bytes(self._queued[:size])
            del self._queued[:size]
        out[:] = pcm.ljust(size, b"\0")


class PcMic:
    """Feeds what the PC's default input hears to every mic `mics()` gives that streams: a mic
    hears it only while it streams, like a real one. Raises `NoAudio` if it can't."""

    def __init__(self, mics: Callable[[], Iterable[SimMic]]) -> None:
        self._mics = mics
        self._loop = asyncio.get_running_loop()
        self._stream = _open("input", self._heard)

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()

    def _heard(self, data: Any, frames: int, time: Any, status: Any) -> None:
        """On PortAudio's thread."""
        self._loop.call_soon_threadsafe(self._feed, bytes(data))

    def _feed(self, pcm: bytes) -> None:
        for mic in self._mics():
            if mic.stream is None:
                continue
            if mic.unheard > _MAX_LAG_BYTES:
                mic.forget()
            mic.feed(pcm)
