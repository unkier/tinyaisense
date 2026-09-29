"""The speech engine interface (ADR 0004): what a plugin implements for each role.

An engine is a Python class that the hub config names by import path, `module:Class`, with
the keyword arguments to make it with. The hub makes and warms up every engine at start, and
calls each role's engines on that role's own thread, one call at a time. Audio is s16le mono
PCM at the engine's `rate`.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass


class Engine:
    """What every engine declares. Set these as class or instance attributes."""

    languages: tuple[str, ...] = ()
    """The languages it handles, e.g. `("en", "ru")`; empty for any."""
    rate: int = 16000
    """Its native sample rate: of the audio it takes, or of the audio it gives."""
    partials: bool = False
    """Whether it gives partial results before its final one."""

    def warm_up(self) -> None:
        """Load what it needs, and run once, so that the first real call is fast. Called on
        the role's thread, once, before any other call."""


class Vad(Engine, ABC):
    """The `vad` role: how likely each window of audio is to be speech."""

    window: int = 512
    """How many samples each call takes."""

    @abstractmethod
    def new_state(self) -> object:
        """The state of a new stream: each stream keeps its own."""

    @abstractmethod
    def probability(self, state: object, pcm: bytes) -> float:
        """How likely `pcm`, `window` samples of a stream, is to be speech: 0 to 1. Updates
        the stream's `state`."""


class Turn(Engine, ABC):
    """The `turn` role: whether the human has finished their turn."""

    @abstractmethod
    def complete(self, pcm: bytes) -> float:
        """How likely the turn is complete, given its latest audio: 0 to 1."""


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    lang: str


class Stt(Engine, ABC):
    """The `stt` role: speech to text."""

    @abstractmethod
    def transcribe(self, pcm: bytes, lang: str | None) -> Transcript:
        """The text of a segment of speech, in `lang` if given, else in the language the
        engine hears."""


class Tts(Engine, ABC):
    """The `tts` role: text to speech."""

    voices: tuple[str, ...] | None = None
    """The engine's own names for the voices it has; None if it takes any name."""

    @abstractmethod
    def synthesize(self, text: str, voice: str) -> Iterable[bytes]:
        """Speak `text` in `voice`. Yields the audio as it's made, in pieces of any length:
        the hub may stop asking for more."""
