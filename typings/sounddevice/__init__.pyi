# The part of sounddevice the Simulator's PC audio uses, which ships no types.

from collections.abc import Callable
from typing import Any

type _Callback = Callable[[Any, int, Any, CallbackFlags], None]

class CallbackFlags: ...
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
