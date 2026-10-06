"""Task 3167: follow-ups of the independent review of task 3164.

* I3164-01: ``_RESOLVED_FINDING_HEADER_RE`` read the rest of a heading line
  from every "(" or "[" in it, so a finding heading followed by 100 KB of
  "(" held the merge gate for 170 s (quadratic; about 11 minutes at 200 KB).
  It now reads each bracket segment once and matches exactly what it
  matched before (compared here on every short token string). The shape is
  timed at 200 KB through the merge gate, and every module-level regex of
  ``loops.py`` is timed on the same bracket-run shapes.
* I3164-02: the shared must-block helpers decide through
  ``dispatch._security_review_blocks_merge``
  (tests/review_gate_production.py). Shown here: with provenance handing
  the parser the normalised text (the R3161-01 regression) the helpers
  fail, and provenance hands the gate the decoded bytes for every character
  normalisation deletes or rewrites.
* I3164-03: the 200 KB timing families are timed through the merge gate
  (tests/test_review_gate_backstop_3143.py and the other timing suites),
  and a review the gate refuses before parsing (a bidi control) still has
  its parse timed.

The source stays ASCII: special characters are named.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import dataclasses
import itertools
import random
import re
import statistics
import time
from pathlib import Path

import pytest

import equipa.dispatch as dispatch
import tests.review_gate_production as review_gate_production
from equipa import loops
from equipa.security_gate import (
    _ANY_LINE_BREAK_RE,
    _BIDI_CONTROL_RE,
    _INVISIBLE_CHARS_RE,
    normalize_review_text,
)
from tests.host_timing import (
    BORDERLINE_GROWTH,
    GROWTH,
    GROWTH_FLOOR_SECONDS,
    GROWTH_LIMIT,
    SettledGrowth,
    assert_linear_time,
    budget,
    settled_growth,
)
from tests.review_gate_production import (
    gate_blocks,
    production_decision,
    production_seconds,
)
from tests.review_gate_timing import TIMING_RUNS, timing_test
from tests.test_review_gate_no_exemptions_3152 import (
    RB,
    assert_backstop_blocks,
    build_review,
)

EM_DASH = "\N{EM DASH}"
ARROW = "\N{RIGHTWARDS ARROW}"

# The regex as it was before task 3167 (introduced by task #3033).
PRE_3167_RESOLVED_FINDING_HEADER_RE = re.compile(
    r"(?:"
    r"[(\[][ \t]*(?i:fixed|resolved)\b[^)\]\n]*[)\]]"
    r"|[(\[][^)\]\n]*\b(?i:not[ \t]+counted)\b[^)\]\n]*[)\]]"
    r"|[" + EM_DASH + "\N{EN DASH}:" + ARROW + r"-][ \t]*[*_]{0,2}"
    r"(?:FIXED|RESOLVED)\b[*_]{0,2}"
    r"(?:[ \t]*[,;][ \t]*[A-Za-z][A-Za-z \t,;-]{0,40})?"
    r"(?:[ \t]*\([^()\n]{0,60}\))?"
    r")[ \t*_.\r]*$",
)
RESOLVED_RE = loops._RESOLVED_FINDING_HEADER_RE

# Every piece the two bracket alternatives and the dash alternative read.
TOKENS = ["(", "[", ")", "]", " ", "x", "\n", "\r", ".", "*", ",", "-",
          EM_DASH, "not counted", "not\tcounted", "n", "counted", "fixed",
          "Resolved", "FIXED"]


def test_the_pre_3167_copy_is_the_old_source():
    """The differential below compares against the regex task 3167 replaced,
    not a paraphrase of it."""
    assert PRE_3167_RESOLVED_FINDING_HEADER_RE.pattern == (
        "(?:[(\\[][ \\t]*(?i:fixed|resolved)\\b[^)\\]\\n]*[)\\]]"
        "|[(\\[][^)\\]\\n]*\\b(?i:not[ \\t]+counted)\\b[^)\\]\\n]*[)\\]]"
        "|[" + EM_DASH + "\N{EN DASH}:" + ARROW
        + "-][ \\t]*[*_]{0,2}(?:FIXED|RESOLVED)\\b"
        "[*_]{0,2}(?:[ \\t]*[,;][ \\t]*[A-Za-z][A-Za-z \\t,;-]{0,40})?"
        "(?:[ \\t]*\\([^()\\n]{0,60}\\))?)[ \\t*_.\\r]*$"
    )


def _mismatches(texts):
    return [text for text in texts
            if bool(RESOLVED_RE.search(text))
            != bool(PRE_3167_RESOLVED_FINDING_HEADER_RE.search(text))]


def test_every_short_token_string_matches_as_before():
    texts = ("".join(combination) for length in range(5)
             for combination in itertools.product(TOKENS, repeat=length))
    assert _mismatches(texts) == []


def test_random_long_token_strings_match_as_before():
    rng = random.Random(3167)
    texts = ["".join(rng.choice(TOKENS) for _ in range(rng.randint(5, 24)))
             for _ in range(40_000)]
    assert _mismatches(texts) == []


@pytest.mark.parametrize("line", [
    "### SR29-00 HIGH (fixed, verified, not counted)",
    "### [2775-S01] HIGH " + EM_DASH + " requestPayout " + ARROW + " **FIXED**",
    "### SR-2996 S1 (MEDIUM) " + EM_DASH + " FIXED, verified",
    "### [S1] HIGH (see S2 [and S3 (not counted)",
    "### [S1] HIGH ((((((fixed in 3101)",
    "### [S1] HIGH [[[ resolved ]",
    "### [S1] HIGH (( a (b not  counted ) ",
])
def test_resolved_headings_still_resolve(line):
    assert RESOLVED_RE.search(line), line
    assert PRE_3167_RESOLVED_FINDING_HEADER_RE.search(line), line


@pytest.mark.parametrize("line", [
    "### [S1] HIGH " + EM_DASH + " nonce (fixed at zero) allows forgery",
    "### [S1] LOW (latent; re-rate MEDIUM when S2 is fixed)",
    "### [S1] HIGH (not counted) then more",
    "### [S1] HIGH (not counted",
    "### [S1] HIGH (cannot counted)",
    "### [S1] HIGH (fixed) (open)",
    "### [S1] HIGH (x) not counted)",
    "### [S1] HIGH " + "(" * 500 + "fixedness)",
    # A bracket group must close the line: these close two groups.
    "### [S1] HIGH (see [S2] and (S3), not counted)",
    "### [S1] HIGH (( a (b not  counted )) ",
])
def test_live_headings_stay_live(line):
    assert not RESOLVED_RE.search(line), line
    assert not PRE_3167_RESOLVED_FINDING_HEADER_RE.search(line), line


# --- I3164-01: linear on bracket runs --------------------------------------

BRACKET_RUNS = {
    "open-parens": "(",
    "open-brackets": "[",
    "paren-bracket": "([",
    "paren-blank": "( ",
    "fixed-groups": "(fixed",
    "not-counted-groups": "(not counted ",
    "word-then-not-counted": "(x not counted",
    "dash-fixed": EM_DASH + "FIXED ",
}


def _search_seconds(pattern, text):
    started = time.process_time()
    pattern.search(text)
    return time.process_time() - started


@timing_test
@pytest.mark.parametrize("name", sorted(BRACKET_RUNS))
def test_a_200kb_heading_tail_is_searched_in_linear_time(name):
    """The old regex took 6.7 s on 20 KB of "(" (quadratic). Budget
    host-calibrated, growth from 50 KB to 200 KB linear (task 3171)."""
    unit = BRACKET_RUNS[name]

    def seconds_at(size):
        text = "### [S1] HIGH " + unit * (size // len(unit))
        return min(_search_seconds(RESOLVED_RE, text) for _ in range(3))

    assert_linear_time(seconds_at, RB, 0.1, name)


@pytest.mark.parametrize("heading", [
    "### [S1] HIGH ", "### [S1] LOW ", "### [S1] CRITICAL ",
])
@pytest.mark.parametrize("unit", ["(", "(["])
@timing_test
def test_the_i3164_01_review_is_decided_by_the_gate_in_half_a_second(
    heading, unit,
):
    """The independent review's shape, through the merge gate: still
    decided as before (a heading the footer does not count), and fast."""
    text = build_review([heading + unit * (RB // len(unit))], "zero")

    decision = production_decision(text)

    assert decision.provenance.trusted, decision.provenance.reason
    assert decision.blocks is True
    assert_linear_time(
        lambda size: production_seconds(
            build_review([heading + unit * (size // len(unit))], "zero")),
        RB, 0.5, f"{heading}{unit}")


# Bracket-run shapes for every module-level regex of loops.py (I3164-01 Fix:
# "scan every other module-level regex with the same harness"), timed the
# way loops.py calls it. A regex it only ever calls with ``.match`` or
# ``.fullmatch`` at a position runs once from that position, so it is timed
# that way; every other one (``.search``, ``.finditer``, ``.sub`` ..., or
# passed on by name) is tried from every position, as the I3164-01 regex
# was. Tried from every position, some anchored-only regexes are quadratic
# (_LINK_TAIL_END_RE, _LINK_POINTY_DESTINATION_RE, _STRICT_COUNTS_LINE_RE,
# _SETEXT_NON_TEXT_RE, _TABLE_DELIMITER_CELL_RE), but loops.py starts each
# at one position per link, line or table cell; the 200 KB timing families
# cover those callers. _SHORTSTAT_RE (quadratic on a digit run) reads only
# the output of git diff --shortstat.
SCAN_PREFIXES = ["", "### [S1] HIGH ", "- **[S1] HIGH** ", "**"]
SCAN_UNITS = ["(", "[", "([", "(not counted ", "(fixed ", "[x",
              EM_DASH + "FIXED ", "( "]
SCAN_BYTES = 50 * 1024
LOOPS_SOURCE = Path(loops.__file__).read_text(encoding="utf-8")


def _module_patterns():
    return {name: value for name, value in vars(loops).items()
            if isinstance(value, re.Pattern)}


def _called_anchored_only(name):
    """True when every use of ``name`` in loops.py (its definition aside) is
    ``.match(`` or ``.fullmatch(`` (one attempt from a given position). A
    bare mention (passed on by name, or a comment) counts as unanchored."""
    uses = re.findall(rf"\b{re.escape(name)}\b(?! = )(\.\w+\()?",
                      LOOPS_SOURCE)
    return bool(uses) and all(use in (".match(", ".fullmatch(")
                              for use in uses)


def test_the_scan_sees_the_gate_regexes():
    patterns = _module_patterns()
    assert len(patterns) >= 100
    assert patterns["_RESOLVED_FINDING_HEADER_RE"] is RESOLVED_RE
    assert not _called_anchored_only("_RESOLVED_FINDING_HEADER_RE")
    assert _called_anchored_only("_LINK_TAIL_END_RE")
    assert not _called_anchored_only("_SHORTSTAT_RE")


def _scan_seconds(pattern, anchored, text):
    started = time.process_time()
    if anchored:
        pattern.match(text)
    else:
        for _ in pattern.finditer(text):
            pass
    return time.process_time() - started


def _best_scan_seconds(pattern, anchored, text, runs=3):
    return min(_scan_seconds(pattern, anchored, text) for _ in range(runs))


SCAN_RUNS = 3
# The larger text of a growth pair stays under this many characters.
MAX_GROWTH_SCAN_CHARACTERS = 4 * 1024 * 1024


def _median_scan_seconds(pattern, anchored, text, runs=SCAN_RUNS):
    return statistics.median(_scan_seconds(pattern, anchored, text)
                             for _ in range(runs))


@dataclasses.dataclass(frozen=True)
class ScanGrowth:
    """The pair of unit counts a scan chose, and its settled readings."""

    quarter_count: int
    settled: SettledGrowth

    @property
    def quarter_seconds(self):
        return self.settled.small_seconds

    @property
    def seconds(self):
        return self.settled.seconds

    @property
    def ratio(self):
        return self.settled.ratio


def scan_growth(pattern, anchored, prefix, unit, count):
    """How a scan of ``prefix + unit * n`` grows from a quarter size to four
    times it (task 3175). A ratio of sub-millisecond scans is noise (CI read
    0.0006 s against 0.0052 s on a linear regex), so the quarter size starts
    at ``count`` units and doubles until the median of ``SCAN_RUNS`` scans
    of it takes ``GROWTH_FLOOR_SECONDS`` (or the larger text would pass
    ``MAX_GROWTH_SCAN_CHARACTERS``). Each size is the median of
    ``SCAN_RUNS`` scans, measured again (keeping the faster median) while
    the ratio is over the limit.

    The size is chosen on a median, not one scan: a linear regex can cost
    more per character past a size, where its state outgrows the memory
    the allocator keeps (``_BLANK_LINE_RUN_RE`` on a newline run holds
    about 150 bytes per newline; past 32 MB, glibc's largest mmap threshold,
    each scan faults its state in afresh: 0.05 s per MB below 256K newlines,
    0.16 s per MB from 1M on, linear on each side). A single noisy scan at
    256K newlines reached the floor under contention while their median was
    0.013 s, so the pair straddled that step and read 12x (task 3175). On
    the median, the quarter size passes the step before it takes the floor,
    and both sizes are measured on the same side of it.

    The pair is settled under contention by the shared helper
    (``settled_growth``, task 3185): over the limit or borderline, both
    sizes are measured again interleaved, each keeping its fastest median,
    so a burst on the quarter size cannot hide quadratic growth."""
    quarter_count = count
    while True:
        quarter_text = prefix + unit * quarter_count
        if (_median_scan_seconds(pattern, anchored, quarter_text)
                >= GROWTH_FLOOR_SECONDS
                or len(quarter_text) * GROWTH * 2
                > MAX_GROWTH_SCAN_CHARACTERS):
            break
        quarter_count *= 2
    def median_at(units):
        return _median_scan_seconds(pattern, anchored, prefix + unit * units)

    return ScanGrowth(quarter_count, settled_growth(
        median_at, quarter_count, quarter_count * GROWTH,
        median_at(quarter_count), median_at(quarter_count * GROWTH)))


def superlinear_scan(pattern, anchored, prefix, unit, count, seconds):
    """The ``ScanGrowth`` of a pair that grows faster than linear, or None.
    ``seconds`` is the pair's time at ``count`` units (a best of three, over
    a screening floor of milliseconds). A first look times one scan of four
    times as many units: linear work reads about 4x and passes. A pair that
    reads ``BORDERLINE_GROWTH`` or more there is decided by ``scan_growth``,
    on settled scans of at least ``GROWTH_FLOOR_SECONDS``, never by the
    first look: a burst that inflated ``seconds`` compresses quadratic
    work's 16x toward linear's 4x, as it did a growth pair's (task 3184)."""
    larger = prefix + unit * (count * GROWTH)
    if _scan_seconds(pattern, anchored, larger) < seconds * BORDERLINE_GROWTH:
        return None
    growth = scan_growth(pattern, anchored, prefix, unit, count)
    return growth if growth.ratio >= GROWTH_LIMIT else None


# A pair whose scan at the test's size takes less than this is fast whatever
# its growth, and is not measured further (the value the shared floor had
# when this scan was written, task 3171).
SCAN_SCREEN_SECONDS = 0.002


@timing_test
def test_no_loops_regex_is_slow_on_a_bracket_run():
    """Each (prefix, unit) pair is held to a host-calibrated 0.1 s at 50 KB
    (best of three), and one that takes measurable time must grow linearly
    from 50 KB on, decided on scans of at least GROWTH_FLOOR_SECONDS
    (``superlinear_scan``; tasks 3171, 3175)."""
    slow = []
    for name, pattern in sorted(_module_patterns().items()):
        anchored = _called_anchored_only(name)
        for prefix, unit in itertools.product(SCAN_PREFIXES, SCAN_UNITS):
            count = SCAN_BYTES // len(unit)
            text = prefix + unit * count
            if _scan_seconds(pattern, anchored, text) < SCAN_SCREEN_SECONDS:
                continue
            elapsed = _best_scan_seconds(pattern, anchored, text)
            if elapsed > budget(0.1):
                slow.append((name, prefix, unit, "budget", round(elapsed, 4)))
                continue
            growth = superlinear_scan(pattern, anchored, prefix, unit, count,
                                      elapsed)
            if growth is not None:
                slow.append((name, prefix, unit, growth))
    assert slow == []


# --- I3164-02: the helpers see what production sees ------------------------

HANGUL_FILLER_BODY = ("Rated\N{HANGUL FILLER}HIGH remote code execution "
                      "in upload.")


def _normalising_provenance(verify):
    """``verify`` with the R3161-01 regression: the verdict's text is the
    normalised review instead of the review as written."""
    def regressed(task_id, review_path):
        verdict = verify(task_id, review_path)
        if verdict.text is None:
            return verdict
        return dataclasses.replace(
            verdict, text=normalize_review_text(verdict.text))
    return regressed


def test_the_shared_helpers_fail_under_the_r3161_01_regression(monkeypatch):
    text = build_review([HANGUL_FILLER_BODY], "zero")
    assert gate_blocks(text)
    assert_backstop_blocks(text)

    for module in (dispatch, review_gate_production):
        monkeypatch.setattr(
            module, "verify_reviewer_provenance",
            _normalising_provenance(module.verify_reviewer_provenance))

    assert not gate_blocks(text)
    with pytest.raises(AssertionError):
        assert_backstop_blocks(text)


def _rewritten_characters():
    """Every character normalisation deletes, apart from the bidi controls
    provenance refuses, plus every line break it rewrites and a BOM."""
    every = "".join(chr(code_point) for code_point in range(0x110000)
                    if not 0xD800 <= code_point <= 0xDFFF)
    deleted = set(_INVISIBLE_CHARS_RE.findall(every))
    deleted -= set(_BIDI_CONTROL_RE.findall(every))
    breaks = set(_ANY_LINE_BREAK_RE.findall(every)) - {"\n"}
    return sorted(deleted | breaks | {"\N{BYTE ORDER MARK}"})


def test_every_timed_review_is_parsed(monkeypatch):
    """I3164-03: ``production_seconds`` times the parse of every review. The
    gate parses a trusted review itself; it refuses a bidi control before
    parsing, so the helper times the parser on that text as well, and a
    family holding one cannot meet its budget on the refusal alone."""
    parsed = []
    analyze = loops._analyze_review_file

    def counting(*args, **kwargs):
        parsed.append(kwargs.get("text"))
        return analyze(*args, **kwargs)

    monkeypatch.setattr(loops, "_analyze_review_file", counting)
    trusted = build_review(["Notes: nothing to report."], "zero")
    refused = build_review(["\N{RIGHT-TO-LEFT OVERRIDE}HGIH"
                            "\N{POP DIRECTIONAL FORMATTING} SQLi"], "zero")

    production_seconds(trusted)
    assert parsed == [trusted] * TIMING_RUNS

    parsed.clear()
    assert production_decision(refused).blocks
    assert parsed == []
    production_seconds(refused)
    assert parsed == [refused] * TIMING_RUNS


def test_provenance_hands_the_gate_the_bytes_as_written(tmp_path):
    rewritten = _rewritten_characters()
    assert {"\r", "\x85", "\N{LINE SEPARATOR}",
            "\N{HANGUL FILLER}", "\N{ZERO WIDTH SPACE}"} <= set(rewritten)
    body = ["Notes: " + "a".join(rewritten) + " high."]
    text = build_review(body, "one_low")
    assert normalize_review_text(text) != text

    decision = production_decision(text, project_dir=tmp_path)

    assert decision.provenance.trusted, decision.provenance.reason
    assert decision.provenance.text == text
    artifact = tmp_path / ".equipa-artifacts" / (
        f"SECURITY-REVIEW-{review_gate_production.GATE_TASK_ID}.md")
    assert artifact.read_bytes().decode("utf-8") == decision.provenance.text
