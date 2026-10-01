#!/usr/bin/env python3
"""Task 3144 RR3138-D: the redaction pre-filter and the htpasswd command scope.

1. The hint pre-filter skips a pattern when none of its lower-case hint words
   appears in the folded text. ``casefold()`` turns U+0130 (capital I with
   dot above) into "i" plus a combining dot, which splits a hint word, while
   ``(?i)`` still matches U+0130 as "i". The review got these leaked (base
   redacted them): ``AUTHOR<U+0130>ZATION: Basic <s>``, ``Set-Cook<U+0130>e:
   sid=<s>`` and ``MAR<U+0130>ADB -p<s>``. The 3138 guard test only checked
   ``letter in fold(char)``, which "i" plus U+0307 satisfies.
2. ``htpasswd -n 'x`` followed by two ``htpasswd -b`` lines: the unclosed
   quote ran the first command to the end of the text, and the htpasswd
   rule redacts only its first match, so the second password leaked.

Every secret below is a FAKE sentinel.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import re
import string
import sys
import time

import pytest

from equipa import redact
from equipa.redact import REDACTED, redact_secrets

DOTTED_I = "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}"
DOTLESS_I = "\N{LATIN SMALL LETTER DOTLESS I}"


def _unhinted(text: str) -> str:
    """redact_secrets without the hint skip: every pattern runs."""
    for pattern, replacement, _hints in redact._PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _ignorecase_letter_equivalents() -> dict[str, list[str]]:
    """ASCII letter -> every non-ASCII character (?i) matches to it."""
    any_letter = re.compile("(?i)[a-z]")
    found: dict[str, list[str]] = {}
    for code in range(128, sys.maxunicode + 1):
        if 0xD800 <= code <= 0xDFFF:
            continue
        char = chr(code)
        if not any_letter.fullmatch(char):
            continue
        for letter in string.ascii_lowercase:
            if re.fullmatch("(?i)" + letter, char):
                found.setdefault(letter, []).append(char)
    return found


EQUIVALENTS = _ignorecase_letter_equivalents()


def test_the_equivalents_include_the_review_letter():
    """The search really sees U+0130, so the guard below exercises it."""
    assert DOTTED_I in EQUIVALENTS["i"]
    assert DOTLESS_I in EQUIVALENTS["i"]


def _hint_variants():
    """Each hint word with one letter replaced by each (?i) equivalent, and
    with every occurrence of that letter replaced."""
    for _pattern, _replacement, hints in redact._PATTERNS:
        for hint in hints:
            for letter, chars in EQUIVALENTS.items():
                if letter not in hint:
                    continue
                positions = [i for i, c in enumerate(hint) if c == letter]
                for char in chars:
                    for position in positions:
                        yield hint, hint[:position] + char + hint[position + 1:]
                    yield hint, hint.replace(letter, char)
                    yield hint, hint.upper().replace(letter.upper(), char)


def test_every_hint_word_survives_folding_with_any_ignorecase_letter():
    """The reviewer's guard: a hint word written with any character (?i)
    matches to its letters, in any case, still contains the hint folded."""
    missed = sorted({(hint, variant) for hint, variant in _hint_variants()
                     if hint not in redact._fold_for_hints(variant)})
    assert not missed, missed[:10]


@pytest.mark.parametrize("text, secret", [
    (f"AUTHOR{DOTTED_I}ZATION: Basic FAKEauthdot3144", "FAKEauthdot3144"),
    (f"author{DOTTED_I}zation: Bearer FAKEauthdot23144", "FAKEauthdot23144"),
    (f"Set-Cook{DOTTED_I}e: sid=FAKEcookdot3144", "FAKEcookdot3144"),
    (f"COOK{DOTTED_I}E: sid=FAKEcookdot23144", "FAKEcookdot23144"),
    (f"MAR{DOTTED_I}ADB -pFAKEmariadot3144", "FAKEmariadot3144"),
    (f"mar{DOTTED_I}adb -uroot -pFAKEmariadot23144", "FAKEmariadot23144"),
    (f"AUTHOR{DOTTED_I}ZAT{DOTTED_I}ON: Basic FAKEauthdot33144",
     "FAKEauthdot33144"),
    (f"pr{DOTTED_I}vate_key: FAKEprivdot3144", "FAKEprivdot3144"),
])
def test_dotted_capital_i_no_longer_skips_the_pattern(text, secret):
    redacted = redact_secrets(text)
    assert secret not in redacted
    assert REDACTED in redacted
    assert redacted == _unhinted(text)


@pytest.mark.parametrize("quote", ["'", '"'])
def test_unclosed_quote_does_not_hide_the_next_htpasswd_line(quote):
    text = (f"htpasswd -n {quote}x\n"
            "htpasswd -b users.db alice FAKEhtpone3144\n"
            "htpasswd -b users.db bob FAKEhtptwo3144")
    redacted = redact_secrets(text)
    assert "FAKEhtpone3144" not in redacted
    assert "FAKEhtptwo3144" not in redacted
    assert redacted.count(REDACTED) == 2


@pytest.mark.parametrize("text, secrets", [
    # Not regressions in the review; they must stay redacted.
    ("mysql 'x\nmysql -pFAKEmysqlq3144", ["FAKEmysqlq3144"]),
    ("docker 'x\ndocker login -p FAKEdockq3144 reg.invalid", ["FAKEdockq3144"]),
    ('sshpass "x\nsshpass -p FAKEsshq3144 ssh host', ["FAKEsshq3144"]),
    # A closed quote may still span lines: the password inside it goes.
    ("mysql -uroot -p'FAKEmulti3144\nFAKEline3144'",
     ["FAKEmulti3144", "FAKEline3144"]),
    # The first line's own password is still found.
    ("htpasswd -b users.db alice 'FAKEhtpq3144\nls -la",
     ["FAKEhtpq3144"]),
])
def test_command_scope_shapes_around_unclosed_quotes(text, secrets):
    redacted = redact_secrets(text)
    for secret in secrets:
        assert secret not in redacted, (secret, redacted)


def test_ordinary_multiline_command_is_left_alone():
    text = "echo 'it\nls -la\ngit status"
    assert redact_secrets(text) == text


SIXTY_FOUR_KB = 64 * 1024


@pytest.mark.parametrize("unit", [
    "htpasswd -n 'x\nhtpasswd -b f u p\n",
    "htpasswd -b '\n",
    'htpasswd -b "\n',
    "mysql 'x\nmysql -pz\n",
    "docker login '\n",
    "htpasswd '",
    "x\n",
])
def test_unclosed_quote_scope_is_linear(unit):
    """A 64 KB text of repeated (un)closed quotes and command words, with a
    final unclosed quote, redacts in well under a second (linear)."""
    text = (unit * (SIXTY_FOUR_KB // len(unit) + 1))[:SIXTY_FOUR_KB - 2] + " '"
    best = min(_timed(redact_secrets, text) for _ in range(2))
    assert best < 0.3, f"{unit!r}: {best:.3f}s"


def test_fold_is_fast_on_64kb():
    text = (f"x{DOTTED_I}{DOTLESS_I}A" * SIXTY_FOUR_KB)[:SIXTY_FOUR_KB]
    assert min(_timed(redact._fold_for_hints, text) for _ in range(2)) < 0.05


def _timed(func, *args) -> float:
    started = time.perf_counter()
    func(*args)
    return time.perf_counter() - started
