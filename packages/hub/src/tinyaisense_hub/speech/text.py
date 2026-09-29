"""The text a `say` speaks: normalized for the TTS, and cut into clauses, so that speech starts
after the first one."""

import re
from dataclasses import dataclass

FIRST_WORDS = 4
"""The first chunk ends at the first clause mark after this many words."""
COMMA_PAST = 120
"""A later chunk ends at a comma only this many chars in."""
CAP = 250
"""No chunk is longer: it's cut at a space."""

_SENTENCE_END = r"[.!?…]+[\"'\u201d\u2019»)\]]*"
_MARK = re.compile(rf"(?:(?P<sentence>{_SENTENCE_END})|(?P<clause>[,;:]))(?=\s|$)")
"""A mark that may end a chunk, followed by a space or the end of the text."""
_WORD = re.compile(r"\S*\w\S*")
_SPACE = re.compile(r"\s+")

_DROPS: tuple[tuple[re.Pattern[str], int | None], ...] = (
    # Code fences, with their language.
    (re.compile(r"^[ \t]*```.*$", re.M), None),
    # Links and images: their text stays.
    (re.compile(r"!?\[([^\]\n]*)\]\([^)\s]*\)"), 1),
    # URLs, less the punctuation after them.
    (re.compile(r"\b(?:https?://|www\.)\S*[^\s.,;:!?'\")\]]"), None),
    # Horizontal rules.
    (re.compile(r"^[ \t]*[-*_=]{3,}[ \t]*$", re.M), None),
    # Headings, quotes and list markers.
    (re.compile(r"^[ \t]*(?:#{1,6}|>+|[-*+•]|\d+[.)])[ \t]+", re.M), None),
    # Emphasis and code marks. An underscore inside a word stays.
    (re.compile(r"\*+|~~|`+|(?<!\w)_+|_+(?!\w)"), None),
    # Emoji, with their modifiers and joiners.
    (
        re.compile(
            "[\U0001f000-\U0001faff\u2300-\u23ff\u2600-\u27bf\u2b00-\u2bff"
            "\ufe0e\ufe0f\u200d\u20e3\U000e0020-\U000e007f]+"
        ),
        None,
    ),
    # Spaces left before punctuation.
    (re.compile(r"\s+(?=[.,;:!?…])"), None),
)
"""What `normalize` drops, in order: a pattern, and the group of it that stays, if any."""


@dataclass(frozen=True, slots=True)
class Clause:
    text: str
    """What the TTS speaks."""
    end: int
    """Where it ends in the original text, which, up to there, is what the human heard once
    it has played. The last clause ends at the end of the text."""


def clauses(text: str) -> list[Clause]:
    """The clauses to speak `text` in: normalized, then `split`."""
    normalized = normalize(text)
    found: list[Clause] = []
    start = 0
    for chunk in split(normalized.text):
        spoken = chunk.rstrip()
        found.append(Clause(spoken, normalized.origin[start + len(spoken) - 1] + 1))
        start += len(chunk)
    if found:
        found[-1] = Clause(found[-1].text, len(text))
    return found


def language(text: str) -> str | None:
    """The language of `text` by its dominant script: `ru` for Cyrillic, else `en`. None if it
    has no letters."""
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return None
    cyrillic = sum("\u0400" <= char <= "\u04ff" for char in letters)
    return "ru" if cyrillic * 2 > len(letters) else "en"


@dataclass(frozen=True, slots=True)
class Normalized:
    text: str
    origin: tuple[int, ...]
    """Where each char of `text` comes from in the original text, then the original's
    length."""


def normalize(text: str) -> Normalized:
    """`text` as the TTS should speak it: no markdown, emoji or URLs, and single spaces."""
    chars = text
    origin = list(range(len(text)))
    for pattern, keep in _DROPS:
        chars, origin = _drop(chars, origin, pattern, keep)
    # Every run of spaces becomes one, where it starts; none at the ends.
    spoken: list[str] = []
    spoken_origin: list[int] = []
    last = len(chars.rstrip())
    for i, char in enumerate(chars[:last]):
        if not char.isspace():
            spoken.append(char)
            spoken_origin.append(origin[i])
        elif spoken and not chars[i - 1].isspace():
            spoken.append(" ")
            spoken_origin.append(origin[i])
    return Normalized("".join(spoken), (*spoken_origin, len(text)))


def _drop(
    text: str, origin: list[int], pattern: re.Pattern[str], keep: int | None
) -> tuple[str, list[int]]:
    parts: list[str] = []
    kept: list[int] = []
    at = 0
    for match in pattern.finditer(text):
        spans = [(at, match.start())]
        if keep is not None:
            spans.append(match.span(keep))
        for start, end in spans:
            parts.append(text[start:end])
            kept += origin[start:end]
        at = match.end()
    parts.append(text[at:])
    kept += origin[at:]
    return "".join(parts), kept


def split(text: str) -> list[str]:
    """Cut `text` into chunks for the TTS, each with the spaces that follow it. Joined, they
    give `text` back.

    The first chunk ends at `, ; :` or a sentence end, after at least `FIRST_WORDS` words;
    later ones at a sentence end, or at a comma past `COMMA_PAST` chars. No chunk is longer
    than `CAP` chars: it's cut after its last space by then, or right at `CAP` if it has none.
    """
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = _chunk_end(text, start, first=not chunks)
        chunks.append(text[start:end])
        start = end
    return chunks


def _chunk_end(text: str, start: int, *, first: bool) -> int:
    """Where the chunk from `start` ends, the spaces after it included."""
    end = next((m.end() for m in _MARK.finditer(text, start) if _ends(m, start, first)), len(text))
    if end - start > CAP:
        spaces = [m.start() for m in _SPACE.finditer(text, start + 1, start + CAP + 1)]
        if not spaces:
            return start + CAP
        end = spaces[-1]
    space = _SPACE.match(text, end)
    return space.end() if space else end


def _ends(mark: re.Match[str], start: int, first: bool) -> bool:
    """Whether `mark` ends the chunk from `start`."""
    if first:
        return len(_WORD.findall(mark.string, start, mark.end())) >= FIRST_WORDS
    return bool(mark["sentence"]) or (mark["clause"] == "," and mark.start() - start > COMMA_PAST)
