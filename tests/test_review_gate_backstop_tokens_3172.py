"""The backstop's severity-word count costs no Python call per word (task
3172, timing on Python 3.10).

``_backstop_tokens`` called ``_backstop_severity`` for every word it counted
and ``_backstop_separated_counts`` (and through it two
``_backstop_mark_separates``) for every marked word of the separated
reading. The 200 KB ``mark_after_each_word`` and ``eta_led`` reviews sat at
0.45 s of their 0.5 s gate budget on Python 3.10, 0.10-0.14 s of it in this
loop. The loop now decides a word's neighbours inline, looks its severity up
by its first character (``_BACKSTOP_SEVERITY_BY_FIRST_CHARACTER``) and
numbers and tallies the counted words in C.

These tests hold it to the pre-3172 loop (a frozen copy) on views full of
marks, to a table built from ``_backstop_severity`` for every character a
token can begin with, and to no per-word helper call through the merge gate.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import bisect
import random

import pytest

from equipa import loops
from tests import test_review_gate_backstop_3143 as backstop
from tests.review_gate_production import production_decision

GONE = loops._BACKSTOP_GONE_MARK
GLUE = loops._BACKSTOP_GLUE_MARK
VIEW_SEED = 3172
VIEW_COUNT = 12_000
WORDS = ("CRITICAL", "HIGH", "MEDIUM")


def fold(letter: str) -> str:
    return chr(loops._BACKSTOP_FOLD_MARK + ord(letter))


def new_fold(letter: str) -> str:
    return chr(loops._BACKSTOP_NEW_FOLD_MARK + ord(letter))


def pre_3172_backstop_tokens(view, origins, *, separated=False):
    """``loops._backstop_tokens`` as it was before task 3172."""
    found = {}
    newlines = None
    token_re = (loops._BACKSTOP_SEPARATED_TOKEN_RE if separated
                else loops._BACKSTOP_TOKEN_RE)
    new_folds = (separated
                 and loops._BACKSTOP_NEW_FOLD_RE.search(view) is not None)
    for match in token_re.finditer(view):
        start, end = match.span()
        if start and (view[start - 1] in loops._BACKSTOP_ASCII_ALNUM
                      or (not separated
                          and view[start - 1] in loops._BACKSTOP_FOLD_MARKS)):
            continue
        if separated:
            if (view[start - 1:start] in loops._BACKSTOP_MARKED_NEIGHBOURS
                    or view[end:end + 1] in loops._BACKSTOP_MARKED_NEIGHBOURS):
                if not loops._backstop_separated_counts(
                    match.group(0), view, start, end, new_folds=new_folds,
                ):
                    continue
            elif not (new_folds and loops._BACKSTOP_NEW_FOLD_RE.search(
                    match.group(0)) is not None):
                continue
        if newlines is None:
            newlines = [line_break.start()
                        for line_break in loops._NEWLINE_RE.finditer(view)]
        severity = loops._backstop_severity(match.group(0))
        line = bisect.bisect_left(newlines, start)
        if origins is not None:
            line = origins[line]
        key = (line, severity)
        found[key] = found.get(key, 0) + 1
    return found


def marked_word(rng: random.Random) -> str:
    """A severity word whose letters may be lookalike marks (I also as l, 1
    or |), with GONE marks between them."""
    pieces = []
    for letter in rng.choice(WORDS):
        if letter == "I" and rng.random() < 0.2:
            letter = rng.choice("l1|")
        roll = rng.random()
        if roll < 0.15 and letter.isalpha():
            letter = fold(letter)
        elif roll < 0.3 and letter.isalpha():
            letter = new_fold(letter)
        pieces.append(letter)
        if rng.random() < 0.1:
            pieces.append(GONE * rng.choice((1, 2)))
    return "".join(pieces)


NEIGHBOURS = [" ", "  ", "\n", "x", "1", "-", "|", "*", GONE, GONE * 2,
              GONE * 64, GONE * 65, GONE * 66, GLUE, fold("a"), fold("H"),
              new_fold("x"), new_fold("I"), "high", "Medium", "HIGHER",
              "CRITICALLY", "MEDIUM2"]


def seeded_views() -> list[str]:
    rng = random.Random(VIEW_SEED)
    views = []
    for _ in range(VIEW_COUNT):
        pieces = []
        for _ in range(rng.randint(1, 12)):
            pieces.append(marked_word(rng) if rng.random() < 0.45
                          else rng.choice(NEIGHBOURS))
        views.append("".join(pieces))
    return views


def origins_for(view: str, rng: random.Random) -> list[int] | None:
    if rng.random() < 0.5:
        return None
    line = 0
    origins = []
    for _ in range(view.count("\n") + 1):
        line += rng.randint(0, 3)
        origins.append(line)
    return origins


@pytest.mark.parametrize("separated", [False, True])
def test_the_count_equals_the_pre_3172_loop(separated):
    rng = random.Random(VIEW_SEED + separated)
    counted = 0
    for view in seeded_views():
        origins = origins_for(view, rng)
        expected = pre_3172_backstop_tokens(view, origins,
                                            separated=separated)
        assert loops._backstop_tokens(view, origins,
                                      separated=separated) == expected, view
        counted += sum(expected.values())
    # The family reaches both outcomes: words counted and words not.
    assert counted > VIEW_COUNT // 10


def test_floods_count_as_before():
    for name in ("mark_after_each_word", "eta_led", "mark_after_each_letter",
                 "small_caps"):
        body = {**backstop.REVIEWER_FAMILIES,
                **backstop.BACKSTOP_FAMILIES}[name][0]
        for view in (body, body.replace(" ", GONE + " "),
                     body.replace(" ", " " + GONE), body.replace(" ", GLUE)):
            for separated in (False, True):
                assert (loops._backstop_tokens(view, None, separated=separated)
                        == pre_3172_backstop_tokens(view, None,
                                                    separated=separated))


def test_every_first_character_of_a_token_has_its_severity():
    """Every BMP character either token regex accepts as a word's first
    character is in the table, with the severity _backstop_severity reads
    from it; the table holds nothing else."""
    tails = {"CRITICAL": "RITICAL", "HIGH": "IGH", "MEDIUM": "EDIUM"}
    firsts = set()
    for code_point in range(0x10000):
        char = chr(code_point)
        for tail in tails.values():
            if any(regex.fullmatch(char + tail) for regex in (
                    loops._BACKSTOP_TOKEN_RE,
                    loops._BACKSTOP_SEPARATED_TOKEN_RE)):
                firsts.add(char)
    assert firsts == set(loops._BACKSTOP_SEVERITY_BY_FIRST_CHARACTER)
    for char, severity in loops._BACKSTOP_SEVERITY_BY_FIRST_CHARACTER.items():
        assert severity == loops._backstop_severity(char)


@pytest.mark.parametrize("name", ["mark_after_each_word", "eta_led"])
def test_no_per_word_helper_runs_through_the_gate(monkeypatch, name):
    """Through the merge gate on a 200 KB flood of marked words: before task
    3172, one _backstop_severity call per counted word (29,257 and 34,133)
    and one _backstop_separated_counts call per marked word of the
    separated reading. The review still blocks."""
    calls = {"_backstop_severity": 0, "_backstop_separated_counts": 0}

    def counted(helper_name):
        helper = getattr(loops, helper_name)

        def wrapper(*args, **kwargs):
            calls[helper_name] += 1
            return helper(*args, **kwargs)
        return wrapper

    for helper_name in calls:
        monkeypatch.setattr(loops, helper_name, counted(helper_name))
    body = {**backstop.REVIEWER_FAMILIES, **backstop.BACKSTOP_FAMILIES}[name]
    decision = production_decision(
        backstop.review("No findings.", body, backstop.ZERO))
    assert decision.provenance.trusted, decision.provenance.reason
    assert decision.blocks
    assert calls == {"_backstop_severity": 0, "_backstop_separated_counts": 0}
