# The part of sounddevice the Simulator's PC audio and the HIL tier's acoustic rig use, which
# ships no types.

from collections.abc import Callable
from typing import Any

type _Callback = Callable[[Any, int, Any, CallbackFlags], None]

class CallbackFlags:
    input_overflow: bool
    output_underflow: bool

class _Time:
    """A callback's times, on PortAudio's clock, in s."""

    inputBufferAdcTime: float
    outputBufferDacTime: float
    currentTime: float

class PortAudioError(Exception): ...

class _RawStream:
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...

class RawOutputStream(_RawStream):
    def __init__(
        self,
        samplerate: float | None = None,
        blocksize: int | None = None,
        device: int | str | None = None,
        channels: int | None = None,
        dtype: str | None = None,
        latency: float | str | None = None,
        *,
        callback: _Callback | None = None,
    ) -> None: ...

class RawInputStream(_RawStream):
    def __init__(
        self,
        samplerate: float | None = None,
        blocksize: int | None = None,
        device: int | str | None = None,
        channels: int | None = None,
        dtype: str | None = None,
        latency: float | str | None = None,
        *,
        callback: _Callback | None = None,
    ) -> None: ...

class _Stream:
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...

type _ArrayCallback = Callable[[Any, int, _Time, CallbackFlags], None]

class InputStream(_Stream):
    def __init__(
        self,
        samplerate: float | None = None,
        blocksize: int | None = None,
        device: int | str | None = None,
        channels: int | None = None,
        dtype: str | None = None,
        latency: float | str | None = None,
        *,
        callback: _ArrayCallback | None = None,
    ) -> None: ...

class OutputStream(_Stream):
    def __init__(
        self,
        samplerate: float | None = None,
        blocksize: int | None = None,
        device: int | str | None = None,
        channels: int | None = None,
        dtype: str | None = None,
        latency: float | str | None = None,
        *,
        callback: _ArrayCallback | None = None,
    ) -> None: ...

def query_devices(device: int | str | None = None, kind: str | None = None) -> Any: ...
