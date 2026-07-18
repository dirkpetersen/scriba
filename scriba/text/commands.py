"""Spoken-command table + matching (DESIGN.md §7.5 point 2).

Commands are recognized only as the *entire* utterance ("standalone") or at
the very end of it ("trailing" -- optionally preceded by "and"/"und") -- never
replaced mid-utterance -- so a literal use of a command word ("the trial
period is 30 days") is never mangled. Matching is case-insensitive on the
final words, after stripping Whisper's own trailing sentence punctuation.

Sentence-end state is NOT tracked here: `casing.py` already derives it purely
from whether the final text ends in `.?!` or `\n`, so a command's output
character is the only signal that needs to reach it.
"""

import re
from dataclasses import dataclass

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class _Command:
    phrase: str  # lowercase, single-spaced
    output: str


_COMMANDS: tuple[_Command, ...] = (
    # English
    _Command("period", "."),
    _Command("full stop", "."),
    _Command("and period", "."),
    _Command("comma", ","),
    _Command("and comma", ","),
    _Command("question mark", "?"),
    _Command("exclamation mark", "!"),
    _Command("new line", "\n"),
    _Command("new paragraph", "\n\n"),
    _Command("colon", ":"),
    _Command("hit enter", "\n"),
    # German
    _Command("punkt", "."),
    _Command("und punkt", "."),
    _Command("komma", ","),
    _Command("und komma", ","),
    _Command("fragezeichen", "?"),
    _Command("ausrufezeichen", "!"),
    _Command("neue zeile", "\n"),
    _Command("neuer absatz", "\n\n"),
    _Command("doppelpunkt", ":"),
)

# Longest phrase (most words) first, so "and period" is tried before "period"
# -- otherwise a trailing "... and period" would match on "period" alone and
# leave a dangling "and" in the remainder.
_COMMANDS_BY_LENGTH = tuple(
    sorted(_COMMANDS, key=lambda c: c.phrase.count(" "), reverse=True)
)


def _normalize_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.strip())


def apply_commands(text: str) -> str:
    """Replaces a standalone or trailing spoken command with its output character(s)."""
    original = _normalize_whitespace(text)
    if not original:
        return text

    core = original.rstrip(".!?")
    orig_words = core.split(" ") if core else []
    lower_words = core.lower().split(" ") if core else []
    if not lower_words:
        return text

    normalized_full = " ".join(lower_words)
    for cmd in _COMMANDS_BY_LENGTH:
        if normalized_full == cmd.phrase:
            return cmd.output

    for cmd in _COMMANDS_BY_LENGTH:
        phrase_words = cmd.phrase.split(" ")
        n = len(phrase_words)
        if 0 < n < len(lower_words) and lower_words[-n:] == phrase_words:
            remainder = " ".join(orig_words[:-n])
            return remainder + cmd.output

    return text
