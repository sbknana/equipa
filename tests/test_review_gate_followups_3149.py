#!/usr/bin/env python3
"""Task 3149: follow-ups of the task 3143 severity-token backstop.

The independent review of 3143 (indep-3143) and EQUIPA's own review
(SECURITY-REVIEW-3143) found that the backstop still merged a real CRITICAL
or HIGH behind an all-zero footer through its zero-count paths. Every row
below fails on the commit before this task.

* R3143-01 / SR3143-01: a heading the parser never counted (inside <p>, after
  "&#35;" decoding) excused itself, its section and its ID.
* R3143-02 / SR3143-02: tally, negation, comparison and list exemptions
  accepted an explicit "<sev>:" or "<sev> -" finding label.
* R3143-03 / SR3143-03: Markdown inline markup split a severity word.
* R3143-04 / SR3143-04, R3143-05: long numeric references and U+0130 raised
  instead of returning a verdict; the gate now turns any parse exception into
  a logged block.
* R3143-06: the reviewer prompt's own prose used UPPER-case severity words;
  MEDIUM-only tokens are counted and logged but no longer block a merge.
* R3143-07: confusable letters and bidi controls.
* R3143-08: the invisible-character list is written with escapes.

Copyright 2026 Forgeborn
"""

import unicodedata
from pathlib import Path

import pytest

from equipa import loops, security_gate
from equipa.security_gate import review_complete_line, reviewer_nonce_line

REPO = Path(__file__).resolve().parent.parent
NONCE = "0123456789abcdef0123456789abcdef"
ZERO = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0"
ONE_LOW = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0"
ONE_MEDIUM = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 1 | LOW: 0 | INFO: 0"
E = "\N{EM DASH}"
LOW_HEADING = f"### [E1] LOW {E} verbose error message"


def review(summary, body, footer, *, heading=None):
    """A finished review: provenance line, Summary, body, Counts, sentinel."""
    lines = [reviewer_nonce_line(NONCE), "# Security Review", "",
             "## Summary", summary, ""]
    if heading:
        lines += [heading, "Details.", ""]
    lines += list(body) + ["", "## Files Reviewed", "- app.py", "",
                           "## Methodology", "Read the diff, ran semgrep.", "",
                           "## Counts", footer, review_complete_line(NONCE)]
    return "\n".join(lines) + "\n"


def analyze(text):
    return loops._analyze_review_file(Path("SECURITY-REVIEW-1.md"), text=text)


def gate_blocks(text):
    """The merge decision of dispatch._security_review_blocks_merge.

    An untrusted review blocks; a trusted one blocks on CRITICAL or HIGH.
    """
    analysis = analyze(text)
    if not analysis.trusted:
        return True
    counts = analysis.counts or {}
    return counts.get("CRITICAL", 0) > 0 or counts.get("HIGH", 0) > 0


def zero_review(body):
    """A zero-finding review: the probes of indep-3143."""
    return review("No findings.", ["## Findings", ""] + list(body), ZERO)


def one_low_review(body):
    """One counted LOW finding: the probes of SECURITY-REVIEW-3143."""
    return review("1 finding.", ["## Notes", ""] + list(body), ONE_LOW,
                  heading=LOW_HEADING)


def assert_gate_blocks(body):
    for text in (zero_review(body), one_low_review(body)):
        assert gate_blocks(text), (body, analyze(text))


def assert_gate_merges(body):
    text = one_low_review(body)
    analysis = analyze(text)
    assert analysis.trusted and not gate_blocks(text), (body, analysis)


# --- R3143-01 / SR3143-01: only headings the parser counted are credited ---------

R3143_01_BODIES = [
    [f"<span></span>### [S2] HIGH {E} see below", "",
     "The HIGH SQL injection in login.py lets anyone log in as admin."],
    [f"<b></b>### [S2] CRITICAL {E} RCE in upload.py"],
    [f"<span>### [S2] HIGH {E} SQL injection in login.py</span>"],
    [f"<p>### [S2] HIGH {E} SQL injection in login.py</p>"],
    [f"<span></span>### [S2] HIGH {E} see below", "", "## Notes", "",
     "See S2: The HIGH SQL injection in login.py lets anyone in."],
    [f"&#35;## [S2] HIGH {E} SQL injection", "",
     "The HIGH SQL injection in login.py lets anyone log in as admin."],
    [f"&#x23;&#x23;&#x23; [S2] CRITICAL {E} RCE in upload.py"],
    [f"<!-- x -->### [S2] HIGH {E} SQL injection in login.py"],
]


@pytest.mark.parametrize("body", R3143_01_BODIES)
def test_r3143_01_uncounted_heading_credits_nothing(body):
    assert_gate_blocks(body)


def test_r3143_01_heading_lines_come_from_the_parser():
    """The credited lines are the parser's own counted headings."""
    text = loops.normalize_review_text(review("1 finding.", [
        "## Findings", "", f"### [S1] MEDIUM {E} Missing rate limit",
        f"<p>### [S2] MEDIUM {E} not a heading</p>",
    ], ONE_MEDIUM))
    analysis = analyze(text)
    lines = text.split("\n")
    counted = loops._counted_heading_lines(text, analysis.heading_offsets)
    assert counted == {(lines.index(f"### [S1] MEDIUM {E} Missing rate limit"),
                        "MEDIUM")}


# --- R3143-08: the invisible-character list uses escapes ------------------------

def test_r3143_08_security_gate_source_holds_no_invisible_character():
    """The PUBLIC repo must not ship raw bidi or zero-width characters."""
    source = (REPO / "equipa" / "security_gate.py").read_text(encoding="utf-8")
    hidden = sorted({
        f"U+{ord(char):04X}" for char in source
        if security_gate._INVISIBLE_CHARS_RE.match(char)
        or unicodedata.category(char) in ("Cf", "Zl", "Zp")
    })
    assert hidden == [], hidden


def test_r3143_08_escaped_list_still_strips_every_format_character():
    missing = [
        f"U+{code_point:04X}" for code_point in range(0x110000)
        if unicodedata.category(chr(code_point)) == "Cf"
        and not security_gate._INVISIBLE_CHARS_RE.match(chr(code_point))
    ]
    assert missing == []
