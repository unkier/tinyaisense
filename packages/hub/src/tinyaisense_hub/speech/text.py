"""The text a `say` speaks: normalized for the TTS, and cut into clauses, so that speech starts
after the first one. The text may come in pieces, from `say.append`."""

import bisect
import re
from dataclasses import dataclass

FIRST_WORDS = 4
"""The first chunk ends at the first clause mark after this many words."""
COMMA_PAST = 120
"""A later chunk ends at a comma only this many chars in."""
CAP = 250
"""No chunk is longer: it's cut at a space."""
IDLE_MS = 500
"""How long streamed text may pause before its tail is spoken anyway."""
IDLE_WORDS = 4
"""An idle tail is spoken only if it has this many words."""

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


class Script:
    """The text of a `say`, cut into clauses as it comes in: each clause as soon as the text
    after it shows where it ends. What follows the clauses is the tail."""

    def __init__(self) -> None:
        self.text = ""
        self.clauses: list[Clause] = []
        self.closed = False
        """No more text comes."""
        self._cut_to = 0
        """Where the clauses so far end in `text`: the tail follows."""

    @classmethod
    def whole(cls, text: str) -> Script:
        """A script that has all its text."""
        script = cls()
        script.append(text)
        script.close()
        return script

    def append(self, text: str) -> None:
        """More text: the clauses it completes are cut."""
        self.text += text
        self._cut(final=False)

    def idle(self) -> None:
        """No text came for `IDLE_MS`: the tail is cut as a clause of its own, if it has
        `IDLE_WORDS` words. A last word that nothing follows yet, not even a mark, may be
        half of one, so it waits for more text."""
        self._cut(final=True, idle=True)

    def close(self) -> None:
        """No more text comes: the tail is the last clause, which ends at the end of the
        text."""
        self._cut(final=True)
        self.closed = True
        if self.clauses:
            self.clauses[-1] = Clause(self.clauses[-1].text, len(self.text))

    def _cut(self, *, final: bool, idle: bool = False) -> None:
        # Normalized from the clauses' last char on, which the patterns look back at: a
        # clause ends at a char that's spoken, never at a space. So the work per call stays
        # bounded by the tail, however long the text.
        start = max(self._cut_to - 1, 0)
        normalized = normalize(self.text[start:])
        at = bisect.bisect_left(normalized.origin, self._cut_to - start, hi=len(normalized.text))
        tail = normalized.text[at:].lstrip()
        at = len(normalized.text) - len(tail)
        # Whether the text holds a space after the tail's last word.
        spaced = (
            bool(tail) and _SPACE.search(self.text, start + normalized.origin[-2] + 1) is not None
        )
        if idle:
            if not spaced and tail[-1:].isalnum():
                tail = tail[: max(tail.rfind(" "), 0)]
            if len(_WORD.findall(tail)) < IDLE_WORDS:
                return
        elif spaced and not final:
            tail += " "
        for chunk in _cut(tail, first=not self.clauses, final=final):
            spoken = chunk.rstrip()
            self._cut_to = start + normalized.origin[at + len(spoken) - 1] + 1
            self.clauses.append(Clause(spoken, self._cut_to))
            at += len(chunk)


def clauses(text: str) -> list[Clause]:
    """The clauses to speak `text` in: normalized, then `split`."""
    return Script.whole(text).clauses


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
    return _cut(text, first=True, final=True)


def _cut(text: str, *, first: bool, final: bool) -> list[str]:
    """`split` `text`, whose first chunk is the first of the whole text if `first`. Unless
    it's `final`, more text may follow: the chunk that the text doesn't show the end of yet
    is left out."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = _chunk_end(text, start, first=first and not chunks, final=final)
        if end is None:
            break
        chunks.append(text[start:end])
        start = end
    return chunks


def _chunk_end(text: str, start: int, *, first: bool, final: bool) -> int | None:
    """Where the chunk from `start` ends, the spaces after it included; None if it's not
    `final` and the text doesn't show that yet."""
    ends = (
        m.end()
        for m in _MARK.finditer(text, start)
        # A mark at the end of the text may be followed by more of the word.
        if _ends(m, start, first) and (final or m.end() < len(text))
    )
    end = next(ends, len(text))
    if end - start > CAP:
        spaces = [m.start() for m in _SPACE.finditer(text, start + 1, start + CAP + 1)]
        if not spaces:
            return start + CAP
        end = spaces[-1]
    elif end == len(text) and not final:
        return None
    space = _SPACE.match(text, end)
    return space.end() if space else end


def _ends(mark: re.Match[str], start: int, first: bool) -> bool:
    """Whether `mark` ends the chunk from `start`."""
    if first:
        return len(_WORD.findall(mark.string, start, mark.end())) >= FIRST_WORDS
    return bool(mark["sentence"]) or (mark["clause"] == "," and mark.start() - start > COMMA_PAST)
