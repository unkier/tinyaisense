"""The fake speech plugin, for tests: text spoken as tones by `FakeTts`, and heard back by
`FakeStt`.

Each character is one 10 ms symbol of two tones, one from a low group and one from a high
group, at whole multiples of 100 Hz so that the symbols of a 10 ms window never blur into each
other. A Voice's register moves the high group, so tests can hear which Voice spoke. Silence
decodes as `_`.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import numpy as np

from .engines import Stt, Transcript, Tts
from .text import language

SYMBOL_MS = 10
ALPHABET = (
    " abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,;:!?'\"-()"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ—…«»"
)
"""The characters it can speak. Any other speaks as `UNKNOWN`."""
UNKNOWN = "\N{REPLACEMENT CHARACTER}"
SILENCE = "_"
REGISTERS = 3

_SYMBOLS = ALPHABET + UNKNOWN
_LOW = range(3, 16)
"""The low group's tones, in 100 Hz bins."""
_HIGH = 12
"""How many tones the high group has, per register."""
_HIGH_FIRST = 17
"""The bin of the high group's first tone, in register 0."""
_AMPLITUDE = 6000
"""Of each tone."""
_SILENT_RMS = 500.0


def encode(text: str, rate: int = 16000, register: int = 0) -> bytes:
    """`text` as tones, s16le at `rate`, one symbol per character."""
    n = rate * SYMBOL_MS // 1000
    t = np.arange(n) / rate
    symbols: list[np.ndarray] = []
    for char in text:
        low, high = divmod(_SYMBOLS.index(char if char in ALPHABET else UNKNOWN), _HIGH)
        low_hz = (_LOW[0] + low) * 100
        high_hz = (_HIGH_FIRST + register * _HIGH + high) * 100
        symbols.append(np.sin(2 * np.pi * low_hz * t) + np.sin(2 * np.pi * high_hz * t))
    samples = np.concatenate(symbols) if symbols else np.zeros(0)
    return np.round(samples * _AMPLITUDE).astype("<i2").tobytes()


@dataclass(frozen=True, slots=True)
class Decoded:
    text: str
    """One character per whole symbol, `SILENCE` for a silent one."""
    registers: frozenset[int]
    """The registers of the Voices heard."""


def decode(pcm: bytes, rate: int = 16000) -> Decoded:
    """The characters that s16le `pcm` at `rate` speaks, symbol by symbol from its start. A
    partial symbol at the end is left out."""
    n = rate * SYMBOL_MS // 1000
    samples = np.frombuffer(pcm, "<i2").astype(np.float64)
    chars: list[str] = []
    registers: set[int] = set()
    for start in range(0, len(samples) - n + 1, n):
        window = samples[start : start + n]
        if np.sqrt(np.mean(window**2)) < _SILENT_RMS:
            chars.append(SILENCE)
            continue
        spectrum = np.abs(np.fft.rfft(window))  # bin k is at k * 100 Hz
        low = int(np.argmax(spectrum[_LOW.start : _LOW.stop]))
        register, high = divmod(
            int(np.argmax(spectrum[_HIGH_FIRST : _HIGH_FIRST + REGISTERS * _HIGH])), _HIGH
        )
        index = low * _HIGH + high
        chars.append(_SYMBOLS[index] if index < len(_SYMBOLS) else UNKNOWN)
        registers.add(register)
    return Decoded("".join(chars), frozenset(registers))


JOURNALS: dict[str, list[str]] = {}
"""What each `FakeTts` with a journal was asked to speak, in order, by journal name."""


class FakeTts(Tts):
    """Speaks text as `encode` does, all of a chunk at once."""

    def __init__(
        self,
        *,
        rate: int = 16000,
        languages: Iterable[str] = ("en", "ru"),
        voices: Mapping[str, int] | None = None,
        journal: str | None = None,
        fail_on: str | None = None,
    ) -> None:
        """`voices` maps each voice name to its register, 0 to 2. With None, every name
        speaks in register 0. With a `journal`, each text it's asked to speak is added to
        `JOURNALS[journal]`, afresh. It fails on any text holding `fail_on`."""
        self.rate = rate
        self.languages = tuple(languages)
        self._registers = dict(voices) if voices is not None else None
        self.voices = tuple(voices) if voices is not None else None
        self._fail_on = fail_on
        self._journal: list[str] | None = None
        if journal is not None:
            self._journal = JOURNALS[journal] = []

    def synthesize(self, text: str, voice: str) -> Iterable[bytes]:
        if self._journal is not None:
            self._journal.append(text)
        if self._fail_on is not None and self._fail_on in text:
            raise RuntimeError(f"failed on {text!r}, as asked")
        register = 0 if self._registers is None else self._registers[voice]
        yield encode(text, self.rate, register)


class FakeStt(Stt):
    """Hears what `FakeTts` said, less the silences."""

    def __init__(self, *, rate: int = 16000, languages: Iterable[str] = ("en", "ru")) -> None:
        self.rate = rate
        self.languages = tuple(languages)

    def transcribe(self, pcm: bytes, lang: str | None) -> Transcript:
        text = decode(pcm, self.rate).text.replace(SILENCE, "")
        return Transcript(text, lang or language(text) or self.languages[0])
