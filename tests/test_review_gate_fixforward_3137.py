#!/usr/bin/env python3
"""Task 3137: fix-forward of the task 3130 review-gate parser.

The independent review of 3130 and EQUIPA's SECURITY-REVIEW-3130 found:

* M1 (SR3130-01): an HTML list item that opens with its own marker
  ("<ol><li>1. HIGH: SQLi</li></ol>") blocked before 3130 and merged after it.
* N1: EQUIPA's own marker comment splitting a word ("HI<!-- EQUIPA-X -->GH")
  made the parser skip the rendered view, so the split word merged.
* F4 leftovers: "Open redirect, severity HIGH in auth.py" and "HIGH SQL
  injection in login handler" merged.
* N2: seven Markdown shapes a CommonMark renderer shows as a finding, but the
  parser hid as code or could not reach.
* N3: a 200 KB review made only of line breaks took about 1 s to parse.

Each shape must now fail closed (count-mismatch) behind an all-zero footer.
Benign prose of the same look must still merge.

Copyright 2026 Forgeborn
"""

import time
from pathlib import Path

import pytest

from equipa import loops
from equipa.security_gate import review_complete_line, reviewer_nonce_line

NONCE = "0123456789abcdef0123456789abcdef"
ZERO = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0"
ONE_LOW = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0"
ONE_LOW_COUNTS = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 1, "INFO": 0}


def review(summary: str, body: list[str], footer: str, low_heading: bool) -> str:
    lines = [reviewer_nonce_line(NONCE), "# Security Review", "",
             "## Summary", summary, ""]
    if low_heading:
        lines += ["### [E1] LOW - verbose error message", "Details.", ""]
    lines += body + ["", "## Files Reviewed", "- app.py", "- tests/test_app.py",
                     "", "## Methodology", "Read the diff, ran semgrep.", "",
                     "## Counts", footer, review_complete_line(NONCE)]
    return "\n".join(lines) + "\n"


def analyze(text: str) -> loops.ReviewCountAnalysis:
    return loops._analyze_review_file(Path("SECURITY-REVIEW-1.md"), text=text)


def assert_blocks_behind_zero_footer(body: list[str],
                                     severity: str = "HIGH") -> None:
    analysis = analyze(review("No findings.", ["## Findings", ""] + body, ZERO,
                              low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, (
        body, analysis.detail)
    # Either a candidate rule saw the severity, or a strict finding heading
    # counted it and the zero footer disagrees.
    assert (f"{severity}=" in analysis.detail
            or analysis.header_counts[severity] > 0), (body, analysis.detail)


def assert_prose_merges(body: list[str]) -> None:
    analysis = analyze(review("1 finding.", ["## Notes", ""] + body, ONE_LOW,
                              low_heading=True))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, (body, analysis.detail)
    assert analysis.counts == ONE_LOW_COUNTS


# --- M1 (SR3130-01): list items that open with their own marker ---------------

@pytest.mark.parametrize("line, severity", [
    # The eight shapes of SECURITY-REVIEW-3130, which blocked before task 3130.
    ("<ol><li>1. HIGH: SQLi in login</li></ol>", "HIGH"),
    ("<ol><li>1) HIGH: SQLi in login</li></ol>", "HIGH"),
    ("<ol><li>2. **HIGH** \N{EM DASH} SQLi</li></ol>", "HIGH"),
    ("<ol><li>10. CRITICAL - RCE in upload</li></ol>", "CRITICAL"),
    ("<ol><li>1. \N{LARGE RED CIRCLE} HIGH: SQLi</li></ol>", "HIGH"),
    ("<ol><li>1. <b>HIGH</b>: SQLi</li></ol>", "HIGH"),
    ("<ul><li>- - HIGH: SQLi</li></ul>", "HIGH"),
    ("<ul><li>> 1. HIGH: SQLi</li></ul>", "HIGH"),
    # Same family: an explicit list item as Markdown inside the HTML item.
    ("<ul><li>* SQLi in login (HIGH)</li></ul>", "HIGH"),
    ("<ol><li>3. SQL injection in search - HIGH</li></ol>", "HIGH"),
])
def test_html_list_item_opening_with_its_own_marker_fails_closed(line, severity):
    assert_blocks_behind_zero_footer([line], severity)


def test_html_list_item_with_its_own_marker_is_counted_once():
    analysis = analyze(review(
        "No findings.", ["<ol><li>1. HIGH: SQLi in login</li></ol>"], ZERO,
        low_heading=False))
    assert analysis.detail.endswith("HIGH=1"), analysis.detail


@pytest.mark.parametrize("line", [
    "<ol><li>1. Test coverage for the parser is high.</li></ol>",
    "<ul><li>- - -</li></ul>",
    "<ol><li>2. High-level design reviewed</li></ol>",
    "<ol><li>1. Overall risk: LOW</li></ol>",
])
def test_html_list_item_with_its_own_marker_prose_merges(line):
    assert_prose_merges([line])


# --- F4 leftovers of the 3122 review ------------------------------------------

@pytest.mark.parametrize("line, severity", [
    ("Open redirect, severity HIGH in auth.py", "HIGH"),
    ("HIGH SQL injection in login handler", "HIGH"),
    # The same shapes in the other positions a finding is written in.
    ("- Open redirect, severity HIGH in auth.py", "HIGH"),
    ("SQL injection (severity CRITICAL) in the search endpoint", "CRITICAL"),
    ("Token leak; severity MEDIUM for the session cookie", "MEDIUM"),
    ("- HIGH SQL injection in login handler", "HIGH"),
    ("1. CRITICAL RCE in the upload handler", "CRITICAL"),
    ("> HIGH SQL injection in login handler", "HIGH"),
    ("\N{LARGE RED CIRCLE} HIGH SQL injection in login handler", "HIGH"),
    ("**HIGH** SQL injection in login handler", "HIGH"),
])
def test_f4_leftover_shapes_fail_closed(line, severity):
    assert_blocks_behind_zero_footer([line], severity)


@pytest.mark.parametrize("line", [
    "HIGH availability is out of scope for this change.",
    "CRITICAL and HIGH findings block the merge.",
    "CRITICAL OR HIGH findings block the merge.",
    "HIGH MEDIUM LOW INFO are the levels the gate reads.",
    "No issues of severity HIGH in this diff.",
    "Findings are ordered, severity HIGH or above first.",
    "Nothing here, severity HIGH and above, was found.",
    "INFO Semgrep finished with no results.",
])
def test_f4_leftover_prose_merges(line):
    assert_prose_merges([line])


# --- N1: EQUIPA's own marker comment splitting a word ---------------------------

@pytest.mark.parametrize("line", [
    # The reviewer's three shapes.
    f"HI<!-- EQUIPA-REVIEWER-RUN: {NONCE} -->GH: SQL injection",
    "- SQL injection in login - H<!--EQUIPA-X-->IGH",
    "### [S1] H<!-- EQUIPA-REVIEW-COMPLETE -->IGH \N{EM DASH} SQL injection",
    # A marker comment that shares its line with text is not standalone.
    f"<!-- EQUIPA-REVIEWER-RUN: {NONCE} -->HIGH: SQL injection",
    f"CRI<!-- EQUIPA-REVIEW-COMPLETE {NONCE} -->TICAL: RCE in upload",
])
def test_marker_comment_inside_a_line_fails_closed(line):
    severity = "CRITICAL" if "TICAL" in line else "HIGH"
    assert_blocks_behind_zero_footer([line], severity)


def test_standalone_marker_comments_still_parse_once(monkeypatch):
    """A clean review whose only comments are the gate's own markers, each on
    its own line, is parsed once (the rendered view adds nothing)."""
    calls = []
    real = loops._analyze_review_text

    def counting(text, nonblank_lines):
        calls.append(text)
        return real(text, nonblank_lines)

    monkeypatch.setattr(loops, "_analyze_review_text", counting)
    analysis = analyze(review("1 finding.", ["## Notes", "", "Plain prose."],
                              ONE_LOW, low_heading=True))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert analysis.counts == ONE_LOW_COUNTS
    assert len(calls) == 1


# --- N2: shapes a CommonMark renderer shows as text -------------------------------

@pytest.mark.parametrize("body", [
    # (a) backtick runs of different lengths do not pair.
    ["SQL injection in login `` ` `` severity: HIGH `x`"],
    ["`` ` `` HIGH: SQL injection in login `x`"],
    # (b) a code span that closes on the next line.
    ["SQL injection in login `x", "y` severity: HIGH `z`"],
    ["See `x", "y` and then:", "HIGH: SQL injection `a` in `b`"],
    # (c) a backtick in the info string: not a fence.
    ["```x`y", "HIGH: SQL injection", "```"],
    # A tab-indented "fence" is indented code, not a fence.
    ["", "\t```", "", "HIGH: SQL injection", "```"],
    # (d) a list item's paragraph indented 4 spaces after a blank line.
    ["- Finding 1", "", "    HIGH: SQL injection"],
    ["1. Finding 1", "", "    HIGH: SQL injection"],
    ["- Findings", "  - Finding 1", "", "      HIGH: SQL injection"],
    # (e) five and more nested blockquotes.
    ["> > > > > HIGH: SQL injection"],
    [">>>>>> - SQL injection (HIGH)"],
    # (f) footnote definitions.
    ["See the note.[^1]", "", "[^1]: HIGH: SQL injection"],
    ["[^note]: - SQL injection in login - HIGH"],
    ["[^rce]: CRITICAL - RCE in upload"],
    # (g) combining marks, typed, as a reference, and precomposed.
    ["H̲IGH: SQL injection"],
    ["H&#818;IGH: SQL injection"],
    ["H\N{LATIN CAPITAL LETTER I WITH ACUTE}GH: SQL injection"],
    # Same family: an escaped backtick, a table cell, an HTML block.
    ["SQL injection in login \\` severity: HIGH `x`"],
    ["| ID | Severity | Note |", "|---|---|---|", "| S1 | `x | HIGH | y` |"],
    ["<div>", "SQL injection ` severity: HIGH ` in login", "</div>"],
    # A backtick inside an inline tag or autolink opens no code span.
    ['See <a href="x`y">the report</a> - severity: HIGH `z'],
    ["See <https://example.test/a`b> - severity: HIGH `c"],
    # A comment that opens mid-line cannot reach into the next list item or
    # across a blank line, so it hides nothing the renderer shows.
    ["- a <!--", "- SQL injection `` ` `` severity: HIGH `x`", "- -->"],
    ["text <!-- x", "", "SQL injection `` ` `` severity: HIGH `x`", "", "-->"],
    # An indented "<!--" is code, not a comment block.
    ["", "        <!--", "", "SQL injection `` ` `` severity: HIGH `x`", "",
     "-->"],
    # A comment joins a word across the lines of one quoted paragraph.
    ["> SQL injection in login", "> HI<!--", "> -->GH: token leak"],
])
def test_markdown_the_renderer_shows_as_text_fails_closed(body):
    severity = "CRITICAL" if any("CRITICAL" in line for line in body) else "HIGH"
    assert_blocks_behind_zero_footer(body, severity)


@pytest.mark.parametrize("body", [
    ["Example: `HIGH: x` is the format."],
    ["```markdown", "HIGH: example finding", "```"],
    ["````", "HIGH: example finding", "```", "still code", "````"],
    ["~~~", "HIGH: example finding", "~~~"],
    ["- Example:", "", "  ```", "  HIGH: example finding", "  ```"],
    ["- Example output:", "", "      HIGH: example finding"],
    ["Example output:", "", "        log.info('HIGH:   ' + message)"],
    ["- Finding context", "", "    The endpoint is internal."],
    ["- - Coverage of the parser is high."],
    ["> > > > > The fix is sound."],
    ["See the advisory.[^1]", "", "[^1]: Upstream advisory, fixed in 2.1."],
    ["Café menu parsing is unchanged."],
    ["A `<!--` quoted in code opens nothing.", "Plain prose.",
     "A `-->` quoted in code closes nothing."],
])
def test_markdown_the_renderer_hides_or_shows_as_prose_merges(body):
    assert_prose_merges(body)


def test_rendered_view_counts_a_nested_finding_once():
    analysis = analyze(review("No findings.", ["- 1. HIGH: SQL injection"],
                              ZERO, low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH
    assert analysis.detail.endswith("HIGH=1"), analysis.detail


def test_honest_review_of_the_new_shapes_merges():
    """A review that writes these shapes and counts them in its footer is
    trusted, with the counts it states."""
    body = ["### [S1] HIGH \N{EM DASH} SQL injection in login", "Details.", "",
            "- Finding 1", "", "    HIGH: SQL injection in login",
            "", "[^1]: HIGH: SQL injection in login"]
    footer = "CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0"
    analysis = analyze(review("1 finding.", body, footer, low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert analysis.counts["HIGH"] == 1


# --- prompt: the format rule claims only what the gate counts --------------------

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "security-reviewer.md"


def _prompt_format_rule() -> str:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    start = text.index("**Finding-shaped lines are counted as findings too.**")
    return text[start:text.index("\n", start)]


def test_prompt_names_the_places_task_3137_counts():
    rule = _prompt_format_rule()
    for place in ("..., severity HIGH in auth.py", "HIGH SQL injection in ...",
                  "- 1. HIGH: ...", "> > > > > HIGH: ...",
                  "a list item's later paragraphs", "[^1]: HIGH: ...",
                  "<li>1. HIGH: ...</li>", "combining marks"):
        assert place in rule, place


def test_prompt_no_longer_claims_what_the_gate_does_not_count():
    rule = _prompt_format_rule()
    # Only the folded lookalikes count (not Coptic or every Cyrillic letter),
    # only one-line HTML counts, only these separators end a list item, and a
    # line opening with a severity needs a separator or a capitalised title.
    assert "lookalike letters from another script" not in rule
    assert "the Greek, Cyrillic, Cherokee or Lisu lookalike letters" in rule
    assert "written in HTML (" not in rule
    assert "one-line HTML" in rule
    assert "a list item that ends with one after a dash, colon or comma" in rule
    assert "starts with a severity (" not in rule
    assert "starts with a severity and a separator" in rule


# --- N3: line-break floods --------------------------------------------------------

REVIEW_BYTES = 200 * 1024


@pytest.mark.parametrize("line_break", ["\n", "\r", "\r\n", " ",
                                        " \n", "\t\n"])
@pytest.mark.parametrize("section", [[], ["## Findings"]])
def test_200kb_of_line_breaks_parses_in_half_a_second(line_break, section):
    # Inside the Summary section, and after a heading that ends it (where
    # every line is also checked for a "Summary:" field).
    body = section + [line_break * (REVIEW_BYTES // len(line_break))]
    text = review("No findings.", body, ZERO, low_heading=False)
    assert len(text.encode()) >= REVIEW_BYTES
    started = time.perf_counter()
    analysis = analyze(text)
    elapsed = time.perf_counter() - started
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert elapsed < 0.5, f"{line_break!r}: {elapsed:.2f}s"


def _padded_lines(line: str) -> list[str]:
    return [line] * (REVIEW_BYTES // (len(line) + 1) + 1)


# Adversarial bodies for every pass and rule task 3137 added. Each is parsed
# as written and as rendered, so each must stay well inside the budget.
ADVERSARIAL_BODIES = {
    "backtick-lines": _padded_lines("`"),
    "backtick-pairs": ["` " * (REVIEW_BYTES // 2)],
    "backtick-run-lengths": [" ".join("`" * length for length in range(1, 640))],
    "unclosed-double-runs": ["`` x " * (REVIEW_BYTES // 5)],
    "escaped-backticks": ["\\`" * (REVIEW_BYTES // 2)],
    "backslash-runs": ["\\" * REVIEW_BYTES + "`"],
    "fence-with-backtick-info": _padded_lines("```x`y"),
    "fence-pairs": _padded_lines("```"),
    "unclosed-tilde-fences": _padded_lines("~~~ x"),
    "comment-lines": _padded_lines("<!--"),
    "midline-comment-openers": _padded_lines("x <!-- y"),
    "comment-in-code": _padded_lines("`<!--` x `-->`"),
    "nested-markers": ["- " * (REVIEW_BYTES // 2) + "HIGH: x"],
    "nested-marker-lines": _padded_lines("- 1. > - x"),
    "deep-blockquotes": [">" * REVIEW_BYTES + " HIGH: x"],
    "list-continuations": ["- a", ""] + _padded_lines("    b"),
    "list-items-and-paragraphs": _padded_lines("- a\n\n    b"),
    "deep-list-nesting": ["  " * depth + "- x" for depth in range(300)] * 3,
    "footnote-lines": _padded_lines("[^1]: x"),
    "table-code-cells": ["| a | b |", "|---|---|"]
    + _padded_lines("| `x | y` | \\| z |"),
    "html-block-lines": ["<div>"] + _padded_lines("`x` HIGHx"),
    "combining-marks": ["H̲" * (REVIEW_BYTES // 3)],
    "accented-letters": ["\N{LATIN CAPITAL LETTER I WITH ACUTE}" * (REVIEW_BYTES // 2)],
    "severity-clause-runs": [", severity " * (REVIEW_BYTES // 11)],
    "title-lead-in-lines": _padded_lines("HIGH HIGH HIGH HIGH Hx"),
    "marker-comment-splits": ["HI<!-- EQUIPA-X -->" * (REVIEW_BYTES // 19)],
    "inline-tags-with-backticks": ["<a title='`'> `x` " * (REVIEW_BYTES // 18)],
    "unclosed-tag-openers": ["<a`" * (REVIEW_BYTES // 3)],
    "long-unclosed-tags": _padded_lines("<a " + "`" * 600),
    # Long blank runs, which folding turns into one blank line, split every
    # possible way by "[ \t]*\**[ \t]*" before task 3137 (quadratic).
    "blank-line-after-heading": ["## Findings", " " * REVIEW_BYTES],
    "tab-line-after-heading": ["## Findings", "\t" * REVIEW_BYTES],
    "bullet-then-blanks": ["## Findings", "- " + " " * REVIEW_BYTES + "x"],
    "hash-then-blanks": ["#" + " " * REVIEW_BYTES + "x"],
    "summary-status-blanks": ["status" + " " * REVIEW_BYTES + "x"],
    "summary-draft-blanks": ["DRAFT" + " " * REVIEW_BYTES + "x"],
    "summary-field-blanks": ["## Notes", "Summary" + " " * REVIEW_BYTES + "x"],
    "marker-comment-blanks": ["<!-- EQUIPA-X" + " " * REVIEW_BYTES + "x"],
    "table-delimiter-blanks": ["| a | b |", "|---" + " " * REVIEW_BYTES + "| x"],
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL_BODIES))
def test_200kb_adversarial_review_parses_under_one_second(name):
    text = review("No findings.", ADVERSARIAL_BODIES[name], ZERO,
                  low_heading=False)
    assert len(text.encode()) >= REVIEW_BYTES
    started = time.perf_counter()
    analyze(text)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"{name}: {elapsed:.2f}s"


@pytest.mark.parametrize("gap", [3, 40, 5000])
def test_folded_blank_lines_keep_findings_and_footer(gap):
    """Folding blank-line runs changes no verdict: a finding far below the
    Summary still blocks, and a footer far below its heading still counts."""
    body = ["## Findings"] + [""] * gap + ["HIGH: SQL injection in login"]
    assert_blocks_behind_zero_footer(body)
    text = review("1 finding.", ["## Notes"] + [""] * gap + ["Plain prose."],
                  ONE_LOW, low_heading=True).replace(
                      "## Counts\n", "## Counts\n" + "\n" * gap)
    analysis = analyze(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert analysis.counts == ONE_LOW_COUNTS
