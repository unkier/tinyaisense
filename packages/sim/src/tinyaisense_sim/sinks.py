"""Where a simulated speaker's audio goes: `speaker.sink = sinks.Wav(path)`.

A sink gets each frame as it enters the DMA, before the volume, with the gain the volume gives
it. `pc.PcSink` plays it on the PC's speaker.
"""

import asyncio
import sys
import wave
from array import array
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .sources import RATE


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    """One frame the speaker played, before its volume."""

    stream: int
    seq: int
    pcm: bytes
    """Silence, a whole frame of it, for a lost frame."""
    at: float
    """When its first sample was heard, on the event loop's clock."""
    lost: bool


class Sink(Protocol):
    def play(self, frame: CapturedFrame, gain: float) -> None:
        """`frame` enters the DMA: it's heard at `frame.at`, scaled by `gain`."""
        ...

    def cut(self, at: float) -> None:
        """Playback was cut at `at`: the frames that would have been heard after it never are."""
        ...

    def close(self) -> None:
        """The device is off."""
        ...


@dataclass(slots=True)
class Capture:
    """Keeps every frame heard since power-on, in memory: the default."""

    frames: list[CapturedFrame] = field(default_factory=list[CapturedFrame])

    def play(self, frame: CapturedFrame, gain: float) -> None:
        self.frames.append(frame)

    def cut(self, at: float) -> None:
        while self.frames and self.frames[-1].at > at:
            self.frames.pop()

    def close(self) -> None:
        pass


class Null:
    """Keeps nothing."""

    def play(self, frame: CapturedFrame, gain: float) -> None:
        pass

    def cut(self, at: float) -> None:
        pass

    def close(self) -> None:
        pass


class Wav:
    """Writes what is heard to a 16 kHz mono WAV file, at the volume: each frame once heard,
    back to back. An underrun's pause leaves no gap."""

    def __init__(self, path: Path) -> None:
        self._file = wave.open(str(path), "wb")  # noqa: SIM115 - open until the device is off
        self._file.setnchannels(1)
        self._file.setsampwidth(2)
        self._file.setframerate(RATE)
        self._dma: deque[tuple[CapturedFrame, float]] = deque()
        """Frames played and not heard yet, with their gain."""

    def play(self, frame: CapturedFrame, gain: float) -> None:
        self._dma.append((frame, gain))
        self._write(asyncio.get_running_loop().time())

    def cut(self, at: float) -> None:
        self._write(at)
        self._dma.clear()

    def close(self) -> None:
        self._write(float("inf"))
        self._file.close()

    def _write(self, by: float) -> None:
        """Write the frames heard by `by`."""
        while self._dma and self._dma[0][0].at <= by:
            frame, gain = self._dma.popleft()
            self._file.writeframes(scale(frame.pcm, gain))


def scale(pcm: bytes, gain: float) -> bytes:
    """s16le `pcm`, scaled by `gain`."""
    if gain == 1.0:
        return pcm
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder == "big":
        samples.byteswap()
    scaled = array("h", (round(s * gain) for s in samples))
    if sys.byteorder == "big":
        scaled.byteswap()
    return scaled.tobytes()
