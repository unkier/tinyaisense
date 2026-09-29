"""The text a `say` speaks: normalized, then cut into clauses for the TTS."""

import pytest
from tinyaisense_hub.speech.text import Clause, Script, clauses, language, normalize, split


def test_the_first_chunk_ends_at_a_clause_mark_after_four_words() -> None:
    assert split("One two three four, five six seven eight, nine.") == [
        "One two three four, ",
        "five six seven eight, nine.",
    ]
    assert split("One two three four; five.") == ["One two three four; ", "five."]
    assert split("One two three four: five.") == ["One two three four: ", "five."]
    assert split("One two three four. Five.") == ["One two three four. ", "Five."]


def test_a_clause_mark_before_the_fourth_word_doesnt_end_the_first_chunk() -> None:
    assert split("Hi, how are you today? Good.") == ["Hi, how are you today? ", "Good."]


def test_later_chunks_end_at_each_sentence_end() -> None:
    assert split("One two three four. Five. Six! Seven? Eight…") == [
        "One two three four. ",
        "Five. ",
        "Six! ",
        "Seven? ",
        "Eight…",
    ]


def test_later_chunks_end_at_a_comma_only_past_120_chars() -> None:
    first = "One two three four. "
    short = "a" * 50 + ", " + "b" * 60 + " end. "
    long = "c" * 121 + ", "

    assert split(first + short + long + "d.") == [first, short, long, "d."]


def test_a_chunk_is_cut_at_a_space_by_250_chars() -> None:
    first = "One two three four. "
    words = " ".join(["word"] * 60) + "."  # 299 chars, no mark but at the end

    chunks = split(first + words)

    assert chunks[0] == first
    assert chunks[1] == " ".join(["word"] * 50) + " "  # 250 chars, the space included
    assert chunks[2:] == [" ".join(["word"] * 10) + "."]


def test_a_chunk_with_no_space_is_cut_at_250_chars() -> None:
    assert split("x" * 300) == ["x" * 250, "x" * 50]


def test_marks_inside_numbers_and_quotes_after_a_sentence_end_are_handled() -> None:
    assert split("Version 3.14 ships at 10:30 today, for 1,000 users. Yes.") == [
        "Version 3.14 ships at 10:30 today, ",
        "for 1,000 users. ",
        "Yes.",
    ]
    assert split('She said "stop it now." Then left.') == [
        'She said "stop it now." ',
        "Then left.",
    ]


def test_normalize_strips_markdown_but_keeps_the_words() -> None:
    assert normalize("**Bold**, __strong__, *it* and _it_ with `code` ~~gone~~").text == (
        "Bold, strong, it and it with code gone"
    )
    assert normalize("snake_case stays").text == "snake_case stays"
    assert normalize("# Title\n\n> quoted\n- one\n* two\n1. three\n---\n").text == (
        "Title quoted one two three"
    )
    assert normalize("```python\nprint(1)\n```").text == "print(1)"
    assert normalize("See [the docs](https://x.io/a) and ![a chart](c.png).").text == (
        "See the docs and a chart."
    )


def test_normalize_strips_urls_and_emoji() -> None:
    assert normalize("Go to https://example.com/a?b=1. Or www.example.org, then.").text == (
        "Go to. Or, then."
    )
    assert normalize("Great 👍🏽 job 🎉! Flags 🇺🇸 and ❤️ too").text == "Great job! Flags and too"


def test_normalize_maps_what_it_keeps_back_to_the_original() -> None:
    original = "**Hi** there 👋 you"
    normalized = normalize(original)

    assert normalized.text == "Hi there you"
    assert [original[i] for i in normalized.origin[:-1]] == list("Hi there you")
    assert normalized.origin[-1] == len(original)


def test_clauses_are_what_the_tts_speaks_and_where_each_ends_in_the_original() -> None:
    text = "**One** two three four, five 🎉 six. [Seven](https://x.io) eight."

    assert clauses(text) == [
        Clause("One two three four,", len("**One** two three four,")),
        Clause("five six.", len("**One** two three four, five 🎉 six.")),
        Clause("Seven eight.", len(text)),
    ]


def test_text_with_nothing_to_speak_has_no_clauses() -> None:
    assert clauses("🎉 https://x.io") == []


def test_the_language_of_a_text_is_its_dominant_script() -> None:
    assert language("Привет, как дела?") == "ru"
    assert language("Hello there") == "en"
    assert language("Hello, мир") == "en"
    assert language("Где, hi") == "ru"
    assert language("Да, hi") == "en"  # a tie isn't dominance
    assert language("Ça va, señor?") == "en"
    assert language("42 + 7 = 49!") is None


def test_streamed_text_is_cut_into_each_clause_as_soon_as_the_text_after_it_shows_its_end() -> None:
    script = Script()

    script.append("One two three four,")
    assert script.clauses == []  # a comma may yet be followed by more of the word
    script.append(" five six")
    assert script.clauses == [Clause("One two three four,", 19)]
    script.append(". Sev")
    assert script.clauses == [Clause("One two three four,", 19), Clause("five six.", 29)]
    script.append("en eight. ")  # the space shows the sentence has ended
    assert script.clauses == [
        Clause("One two three four,", 19),
        Clause("five six.", 29),
        Clause("Seven eight.", 42),
    ]
    script.close()
    assert script.clauses[-1] == Clause("Seven eight.", 43)  # the last ends at the text's end
    assert script.text == "One two three four, five six. Seven eight. "


@pytest.mark.parametrize(
    "text",
    [
        "**One** two three four, five 🎉 six. See https://x.io/a, then [Seven](c.png) eight.",
        "Version 3.14 ships at 10:30 today, for 1,000 users. Yes!\n\n- one, two\n1. three",
        "One two three four. " + " ".join(["word"] * 60) + ". Done.",
        "Привет, как твои дела? Хорошо… Мои — нет.",
    ],
)
def test_streamed_text_is_cut_as_the_whole_text_is_however_it_comes_in(text: str) -> None:
    whole = clauses(text)
    for at in range(len(text) + 1):
        halves = Script()
        halves.append(text[:at])
        halves.append(text[at:])
        halves.close()
        assert halves.clauses == whole, at
    chars = Script()
    for char in text:
        chars.append(char)
    chars.close()
    assert chars.clauses == whole


def test_an_idle_tail_is_cut_as_a_clause_if_it_has_four_words_less_a_half_sent_last_one() -> None:
    script = Script()
    script.append("Well, one two three four. Five **six** 🎉")

    script.idle()
    assert script.clauses == [Clause("Well, one two three four.", 25)]
    script.append(" seven eigh")
    script.idle()  # "eigh" may be half of a word: that leaves three
    assert script.clauses[1:] == []
    script.append("t nine")
    script.idle()
    assert script.clauses[1:] == [Clause("Five six seven eight", 52)]
    script.append(" ten eleven twelve.")
    script.idle()  # the full stop ends the last word
    assert script.clauses[2:] == [Clause("nine ten eleven twelve.", 76)]
