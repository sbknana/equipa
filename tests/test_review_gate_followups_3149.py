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


# --- R3143-02 / SR3143-02: no exemption may excuse a finding label ---------------

# Every body of indep-3143's attack table and SECURITY-REVIEW-3143's SR3143-02,
# then the same shapes with the other separators a label uses.
R3143_02_BODIES = [
    ["- Not MEDIUM, HIGH: SQL injection in login.py allows auth bypass"],
    ["Downgraded: not CRITICAL, HIGH: SQL injection in login.py."],
    ["Severity: not MEDIUM, HIGH"],
    ["- Rated higher than MEDIUM, HIGH: SQL injection in login.py"],
    [f"- Severity worse than HIGH {E} RCE in upload handler"],
    [f"- Neither LOW nor MEDIUM, CRITICAL {E} RCE via upload"],
    ["- 0 MEDIUM, CRITICAL: RCE via upload handler"],
    ["0 CRITICAL / HIGH: SQL injection in login.py lets anyone in."],
    ["CRITICAL: 0 rate limiting on login enables credential stuffing"],
    ["HIGH: 0 auth checks on /admin let anyone delete users."],
    ["Was it fixed? No HIGH SQL injection remains in login.py"],
    ["- not exploitable remotely? No. HIGH \N{EM DASH} SQL injection in login.py"],
    ["<!-- no --> HIGH: SQL injection in login.py"],
    ["<span>no</span> HIGH: SQL injection in login.py"],
    ["[x](no) HIGH: SQL injection in login.py"],
    ['[x](y "no") HIGH: SQL injection in login.py'],
    ["None of these are HIGH except: SQL injection in login.py (HIGH)"],
    # Variants: dashes, closing emphasis, zero tallies, table cells.
    ["No HIGH - SQL injection in login.py"],
    ["No HIGH \N{EN DASH} SQL injection in login.py"],
    ["No HIGH\N{EM DASH}SQL injection in login.py"],
    ["**No HIGH**: SQL injection in login.py"],
    ["- 0 HIGH: SQL injection in login.py"],
    [f"- 0 HIGH {E} SQL injection in login.py"],
    ["Zero CRITICAL: RCE via upload handler"],
    ["Without HIGH (SQL injection in login.py)"],
    ["Kept at LOW rather than HIGH: SQL injection in login.py"],
    ["HIGH: 0 in login.py lets anyone in"],
    ["HIGH = 0 auth checks on /admin"],
    ["| HIGH | 0 auth checks on /admin |"],
    ["| finding | HIGH: 0 rate limiting on login |"],
    ["CRITICAL: 0-click RCE in upload"],
    ["Severity: not MEDIUM, HIGH."],
    ["not LOW, MEDIUM, HIGH"],
    # A negation at the end of the line before (soft wrap) is no excuse
    # either when the token labels a finding.
    ["Was it fixed? No", "HIGH SQL injection remains in login.py"],
    ["Was it fixed? No", "HIGH: SQL injection in login.py"],
    ["# Was it fixed? No", "HIGH findings remain."],
]


@pytest.mark.parametrize("body", R3143_02_BODIES)
def test_r3143_02_exemptions_never_excuse_a_label(body):
    assert_gate_blocks(body)


# Tallies and negations a reviewer writes, which still merge (none of these
# leaves an unaccounted token, MEDIUM included).
R3143_02_MERGES = [
    ["Totals: 0 CRITICAL / 0 HIGH / 0 MEDIUM / 1 LOW / 0 INFO."],
    ["semgrep: CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 across the diff."],
    ["| Severity | Count |", "|---|---|", "| HIGH | 0 |", "| CRITICAL | 0 |"],
    ["**No CRITICAL or HIGH findings.**"],
    ["There are no CRITICAL, HIGH or MEDIUM issues."],
    ["There are no CRITICAL, HIGH, or MEDIUM findings."],
    ["None is CRITICAL or HIGH."],
    ["Rated LOW, not MEDIUM, as it needs a local account."],
    ["Kept at LOW rather than MEDIUM."],
    ["0 CRITICAL/HIGH results from semgrep."],
    ["HIGH: 0 and CRITICAL: 0 from semgrep."],
    ["| semgrep | HIGH: 0 |"],
    ["No HIGH-severity issues."],
    ["No HIGH or CRITICAL issues were found."],
    ["Rated LOW rather than MEDIUM or HIGH."],
]


@pytest.mark.parametrize("body", R3143_02_MERGES)
def test_r3143_02_plain_tallies_and_negations_still_merge(body):
    analysis = analyze(one_low_review(body))
    assert analysis.trusted and analysis.detail == "", (body, analysis)


def test_r3143_06_soft_wrapped_negation_merges():
    """The corpus shape: "No" ends the line, the list opens the next one."""
    body = ["The diff touches only the parser, and the scan found no",
            "CRITICAL/HIGH/MEDIUM issues. Remaining items are LOW/INFO."]
    analysis = analyze(one_low_review(body))
    assert analysis.trusted and analysis.detail == "", analysis


@pytest.mark.parametrize("before", [
    "## Findings: no",          # a heading is not a paragraph line
    "| scan | no",              # neither is a table row
    "",                         # a blank line ends the paragraph
])
def test_r3143_06_wrapped_negation_needs_a_paragraph_line(before):
    body = [before, "CRITICAL issues in login.py."]
    assert gate_blocks(one_low_review(body)), body


# --- R3143-04 / SR3143-04, R3143-05: no input raises -----------------------------

@pytest.mark.parametrize("reference", [
    "&#" + "9" * 5000,
    "&#" + "9" * 5000 + ";",
    "&#" + "1" * 4301,
    "&#x" + "F" * 5000 + ";",
    "&#" + "0" * 5000 + "72;",
])
def test_r3143_04_long_numeric_reference_returns_a_verdict(reference):
    text = one_low_review([f"See {reference} in the log."])
    analysis = analyze(text)   # raised ValueError before task 3149
    assert analysis.verdict in (loops.REVIEW_VERDICT_OK,
                                loops.REVIEW_VERDICT_COUNT_MISMATCH)


def test_r3143_04_leading_zeros_still_decode_to_a_letter():
    """A browser reads every leading zero: "&#000000072;" is H."""
    assert_gate_blocks(["&#" + "0" * 40 + "72;IGH: SQL injection in login.py"])


@pytest.mark.parametrize("line", [
    "- H\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}GH: SQL injection in login.py",
    "### [S2] H\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}GH \N{EM DASH} SQLi",
    "| SQL injection | H\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}GH |",
])
def test_r3143_05_dotted_capital_i_blocks_instead_of_raising(line):
    assert_gate_blocks(["## Findings", "", line])


def test_parse_error_becomes_a_logged_block(monkeypatch, tmp_path):
    """ANY parser exception is a count-mismatch with a logged reason."""
    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    logged = []
    monkeypatch.setattr(loops, "_analyze_review_file", explode)
    monkeypatch.setattr(loops, "_gate_audit_log",
                        lambda message, **fields: logged.append((message, fields)))
    path = tmp_path / "SECURITY-REVIEW-7.md"
    counts = loops._count_findings_in_review_file(
        path, task_id=7, text=one_low_review([]))
    assert counts is None
    assert len(logged) == 1, logged
    message, fields = logged[0]
    assert fields["event"] == "count-mismatch"
    assert "review parse error: RuntimeError" in message
    assert "action=treat-as-missing" in message


def test_r3143_04_old_crash_input_blocks_with_a_logged_verdict(monkeypatch, tmp_path):
    """The R3143-04 input end to end through the gate's counting call."""
    logged = []
    monkeypatch.setattr(loops, "_gate_audit_log",
                        lambda message, **fields: logged.append(message))
    text = one_low_review(["HIGH &#" + "9" * 5000])
    assert loops._count_findings_in_review_file(
        tmp_path / "SECURITY-REVIEW-8.md", task_id=8, text=text) is None
    assert any("unaccounted severity token" in message for message in logged)


# --- R3143-06: MEDIUM-only tokens are counted, never a block ---------------------

def test_r3143_06_medium_only_tokens_are_counted_and_logged(monkeypatch,
                                                           tmp_path):
    logged = []
    monkeypatch.setattr(loops, "_gate_audit_log",
                        lambda message, **fields: logged.append((message, fields)))
    text = one_low_review(["The cache has a MEDIUM issue: stale entries."])
    counts = loops._count_findings_in_review_file(
        tmp_path / "SECURITY-REVIEW-9.md", task_id=9, text=text)
    assert counts is not None, "MEDIUM never blocks a merge"
    assert counts["MEDIUM"] == 1 and counts["HIGH"] == counts["CRITICAL"] == 0
    events = [fields["event"] for _, fields in logged]
    assert events == ["backstop-advisory"], logged
    assert "MEDIUM=1 at line" in logged[0][0]


def test_r3143_06_medium_with_high_still_blocks_and_names_both():
    text = one_low_review(["A MEDIUM issue and a HIGH one: SQL injection."])
    analysis = analyze(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH
    assert analysis.detail.startswith(loops.BACKSTOP_REASON + ":")
    assert "HIGH=1" in analysis.detail and "MEDIUM=1" in analysis.detail


def test_merge_blocking_severities_match_the_gate_policy():
    """The advisory rule rests on the gate blocking on CRITICAL/HIGH only."""
    source = (REPO / "equipa" / "dispatch.py").read_text(encoding="utf-8")
    assert ('blocks = counts.get("CRITICAL", 0) > 0 or '
            'counts.get("HIGH", 0) > 0') in source
    assert loops.MERGE_BLOCKING_SEVERITIES == ("CRITICAL", "HIGH")


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
