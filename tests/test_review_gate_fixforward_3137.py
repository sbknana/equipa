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

import random
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


# Marker comments on their own line and inside a word, mixed with every block
# shape that changes how the next line renders (lists, quotes, fences, code,
# comments, HTML blocks, footnotes, setext underlines, tables).
SHORTCUT_LINE_POOL = [
    "", "", "",
    f"<!-- EQUIPA-REVIEWER-RUN: {NONCE} -->", "<!-- EQUIPA-X -->",
    "   <!-- EQUIPA-REVIEW-COMPLETE -->",
    f"<!-- EQUIPA-REVIEW-COMPLETE {NONCE} -->",
    "HI<!-- EQUIPA-X -->GH: SQL injection",
    "- SQL injection in login - H<!--EQUIPA-X-->IGH",
    "- Finding 1", "1. Finding 1", "> quoted", "> > > > >",
    "    HIGH: SQL injection", "  HIGH: SQL injection", "HIGH: SQL injection",
    "- SQL injection - HIGH", "HIGH SQL injection in login",
    "```", "~~~", "    code", "\tcode", "<div>", "</div>", "<!--", "-->",
    "Plain prose.", "===", "---", "[^1]: note", "[^1]:", "| a | b |",
    "|---|---|", "`", "``", "<ol><li>1. note</li></ol>", "<li>", "</li>",
    "## Findings", "### [S1] HIGH \N{EM DASH} SQL injection",
]


def _parse_both_views(text: str) -> loops.ReviewCountAnalysis:
    """``text`` parsed as written AND as rendered, with no shortcut."""
    text = loops.normalize_review_text(text)
    nonblank_lines = sum(1 for line in text.splitlines() if line.strip())
    text = loops._fold_blank_line_runs(text)
    rendered = loops._rendered_review_text(text)
    return loops._stricter_analysis(
        loops._analyze_review_text(text, nonblank_lines),
        loops._analyze_review_text(rendered, nonblank_lines),
    )


def test_parsing_once_gives_the_verdict_of_both_views():
    """Whenever the rendered view is skipped, parsing it would have changed
    nothing: same verdict, same counts, over seeded random reviews. Before
    task 3137 a marker comment inside a word took the shortcut and merged
    reviews that parsing both views blocks."""
    rng = random.Random(3137)
    for _ in range(1000):
        body = [rng.choice(SHORTCUT_LINE_POOL)
                for _ in range(rng.randint(1, 10))]
        text = review("No findings.", body, ZERO, low_heading=False)
        parsed_once = analyze(text)
        both_views = _parse_both_views(text)
        assert (parsed_once.verdict, parsed_once.counts) == (
            both_views.verdict, both_views.counts), (body, both_views.detail)


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
    # A footnote's later paragraph (GFM: indented 4), and a footnote inside a
    # quote or list item.
    ["[^1]: Finding 1", "", "    HIGH: SQL injection"],
    ["> [^1]: HIGH: SQL injection"],
    ["> > [^note]: - SQL injection in login - HIGH"],
    # (g) combining marks, typed, as a reference, and precomposed.
    ["H̲IGH: SQL injection"],
    ["H&#818;IGH: SQL injection"],
    ["H\N{LATIN CAPITAL LETTER I WITH ACUTE}GH: SQL injection"],
    # An enclosing mark (category Me) draws around the letter, typed and as
    # a reference.
    ["H\N{COMBINING ENCLOSING CIRCLE}IGH: SQL injection"],
    ["H&#8413;IGH: SQL injection"],
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
    ["[^1]: Upstream advisory.", "", "    Fixed in 2.1, see the changelog."],
    # Indented 8 under a footnote is indented code inside it.
    ["[^1]: Example output:", "", "        HIGH: example finding"],
    ["> [^1]: Upstream advisory, fixed in 2.1."],
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


# Every place the rule names, in every nested position and spelling it says
# still counts ("Those line and list-item shapes also count inside nested
# lists and quotes ..., in a list item's later paragraphs and after a
# footnote label", "All of these count when written in one-line HTML ...").
PROMPT_SPELLINGS_OF_HIGH = {
    "plain": "HIGH",
    "decimal-reference": "&#72;IGH",
    "hex-reference": "&#x48;IGH",
    "named-reference": "&Eta;IGH",
    "combining-mark": "H\N{COMBINING LOW LINE}IGH",
    "comment-split": "HI<!-- x -->GH",
    "greek": "\N{GREEK CAPITAL LETTER ETA}\N{GREEK CAPITAL LETTER IOTA}GH",
    "cyrillic": "\N{CYRILLIC CAPITAL LETTER EN}"
                "\N{CYRILLIC CAPITAL LETTER BYELORUSSIAN-UKRAINIAN I}GH",
    "cherokee": "\N{CHEROKEE LETTER MI}IGH",
    "lisu": "\N{LISU LETTER XA}\N{LISU LETTER I}\N{LISU LETTER GA}H",
    "small-capitals": "\N{LATIN LETTER SMALL CAPITAL H}"
                      "\N{LATIN LETTER SMALL CAPITAL I}"
                      "\N{LATIN LETTER SMALL CAPITAL G}"
                      "\N{LATIN LETTER SMALL CAPITAL H}",
}
PROMPT_LINE_SHAPES = [
    "SQL injection in login, severity {S}, auth.py",
    "Open redirect, severity {S} in auth.py", "The severity is {S}.",
    "SQL injection, rated {S}.", "Risk: {S}", "Sev: {S}", "Impact: {S}",
    "Priority: {S}", "Rating: {S}", "SQL injection in login. Risk: {S}.",
    "{S}: SQL injection", "S1: {S}", "[{S}] SQL injection",
    "[S1] {S} SQL injection", "{S} SQL injection in login handler",
]
PROMPT_LIST_ITEM_SHAPES = [
    "- {S}: SQL injection", "- Risk: {S}", "- SQL injection - {S}",
    "- SQL injection: {S}", "- SQL injection, {S}", "- SQL injection ({S})",
    "- SQL injection [{S}]",
]
PROMPT_OTHER_SHAPES = [
    ["#### {S} SQL injection"], ["#### {S}-severity SQL injection"],
    ["{S} SQL injection", "==="], ["{S} SQL injection", "---"],
    ["<h4>{S} SQL injection</h4>"], ["**{S}** SQL injection"],
    ["| ID | Severity |", "|---|---|", "| S1 | {S} |"],
    ["| ID | Severity |", "|---|---|", "| S1 | {S}/MEDIUM |"],
    ["| ID | Finding |", "|---|---|", "| S1 | {S}: SQLi |"],
    ["| ID | Finding |", "|---|---|", "| S1 | Severity: {S} |"],
    ["| ID | Finding |", "|---|---|", "| S1 | Risk: {S} |"],
    ["<table><tr><td>S1</td><td>{S}</td></tr></table>"],
    ["<details><summary>{S}: SQL injection</summary></details>"],
    ["<b>{S}</b> SQL injection"],
]


def _prompt_positions(line: str, is_list_item: bool) -> list[list[str]]:
    item = line[2:] if is_list_item else line
    return [
        [line],
        ["- " + line] if is_list_item else ["- 1. " + item],
        ["> > > > > " + line],
        ["- Finding 1", "", "    " + line],
        ["[^1]: " + line],
        ["<ul><li>" + item + "</li></ul>"],
        ["<ol><li>1. " + item + "</li></ol>"],
        ["Finding 1<br>" + line],
    ]


def _counts_as_high(body: list[str]) -> bool:
    analysis = analyze(review("No findings.", ["## Findings", ""] + body, ZERO,
                              low_heading=False))
    return analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH


@pytest.mark.parametrize("spelling", sorted(PROMPT_SPELLINGS_OF_HIGH))
def test_every_place_the_prompt_names_counts_in_every_spelling_it_names(
        spelling):
    word = PROMPT_SPELLINGS_OF_HIGH[spelling]
    bodies = [[line.replace("{S}", word) for line in shape]
              for shape in PROMPT_OTHER_SHAPES]
    for shapes, is_list_item in ((PROMPT_LINE_SHAPES, False),
                                 (PROMPT_LIST_ITEM_SHAPES, True)):
        for shape in shapes:
            bodies += _prompt_positions(shape.replace("{S}", word), is_list_item)
    missed = [body for body in bodies if not _counts_as_high(body)]
    assert not missed, missed


@pytest.mark.parametrize("body", [
    ["#### High-severity SQL injection"],
    ["<h3>High-severity SQL injection</h3>"],
    ["| ID | Note |", "|---|---|", "| S1 | High risk |"],
    ["| ID | Note |", "|---|---|", "| S1 | Critical impact |"],
])
def test_title_case_places_the_prompt_names_count(body):
    assert _counts_as_high(body), body


# --- N3: line-break floods --------------------------------------------------------

REVIEW_BYTES = 200 * 1024


@pytest.mark.parametrize("line_break", [
    "\n", "\r", "\r\n", "\N{LINE SEPARATOR}", " \n", "\t\n",
    # Every other character str.splitlines() breaks a line on. Before task
    # 3137 each single-byte one took about 1 s per 200 KB, like "\n".
    "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85",
    "\N{PARAGRAPH SEPARATOR}",
])
@pytest.mark.parametrize("section", [[], ["## Findings"]])
def test_200kb_of_line_breaks_parses_in_half_a_second(line_break, section):
    # Inside the Summary section, and after a heading that ends it (where
    # every line is also checked for a "Summary:" field).
    # Sized in UTF-8 bytes, so a U+2028 flood is 200 KB like the others.
    body = section + [line_break * (REVIEW_BYTES // len(line_break.encode()))]
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


# Found by a generated search over token pairs (a + b*n at 4 KB and 16 KB):
# blanks after a bold opener were split every way between two "[ \t]*"
# runs in the bold lead-in rule, so 16 KB took 3 s and 200 KB minutes. "<b>"
# is rewritten as "**", so the HTML rescan paid it too.
BOLD_OPENER_BYTES = 32 * 1024


@pytest.mark.parametrize("line", [
    "**" + " " * BOLD_OPENER_BYTES + "x",
    "- **" + "\t" * BOLD_OPENER_BYTES,
    "<b>" + " " * BOLD_OPENER_BYTES,
    "<strong>" + "\N{NO-BREAK SPACE}" * BOLD_OPENER_BYTES,
    "<b>" + "`` " * (BOLD_OPENER_BYTES // 3),
])
def test_blanks_after_a_bold_opener_parse_in_linear_time(line):
    text = review("No findings.", ["## Findings", "", line], ZERO,
                  low_heading=False)
    started = time.perf_counter()
    analysis = analyze(text)
    elapsed = time.perf_counter() - started
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert elapsed < 0.5, f"{line[:12]!r}: {elapsed:.2f}s"


@pytest.mark.parametrize("line, severity", [
    ("**  HIGH** \N{EM DASH} SQL injection in login", "HIGH"),
    ("**( HIGH )** SQL injection in login", "HIGH"),
    ("** [ \tCRITICAL ]** RCE in upload", "CRITICAL"),
    ("- ** (MEDIUM)** Token leak", "MEDIUM"),
    ("<b>  (HIGH)</b> SQL injection in login", "HIGH"),
])
def test_bold_lead_ins_with_blanks_and_brackets_still_fail_closed(line, severity):
    assert_blocks_behind_zero_footer([line], severity)


# Found by a generated search over token pairs: a row of 200 KB of empty
# cells ran all six table severity-cell rules on every cell, in every scan of
# both views. A marker comment that shares its line forces both views, so it
# took about 1 s. A row without a severity word now skips the rules.
PIPE_FLOODS = {
    "pipes": "|" * REVIEW_BYTES,
    "marker-comment-then-pipes": "<!-- EQUIPA-X -->" + "|" * REVIEW_BYTES,
    "pipes-then-marker-comment": "|" * REVIEW_BYTES + "<!-- EQUIPA-X -->",
    "tilde-fence-then-pipes": "~~~" + "|" * REVIEW_BYTES,
    "spaced-cells": "| " * (REVIEW_BYTES // 2),
    "word-cells": "| x " * (REVIEW_BYTES // 4),
}


@pytest.mark.parametrize("section", [[], ["## Findings"]])
@pytest.mark.parametrize("name", sorted(PIPE_FLOODS))
def test_200kb_row_of_cells_without_a_severity_parses_in_half_a_second(
        name, section):
    text = review("No findings.", section + [PIPE_FLOODS[name]], ZERO,
                  low_heading=False)
    started = time.perf_counter()
    analysis = analyze(text)
    elapsed = time.perf_counter() - started
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert elapsed < 0.5, f"{name}: {elapsed:.2f}s"


# Found by the same search: on a line that opens an HTML comment block, every
# later comment on the line rescanned the line from its start to see whether
# it was the block's opener, so 200 KB of comments took about 0.6 s and
# 512 KB 3.5 s (quadratic).
COMMENT_LINE_FLOODS = {
    "marker-comments-then-backticks":
        "<!-- EQUIPA-X -->" * (REVIEW_BYTES // 17) + "``",
    "comments": "<!-- x -->" * (REVIEW_BYTES // 10),
    "indented-comments": "   " + "<!-- x -->" * (REVIEW_BYTES // 10),
    "comments-then-unclosed": "<!-- x -->" * (REVIEW_BYTES // 10) + "<!--",
}


@pytest.mark.parametrize("section", [[], ["## Findings"]])
@pytest.mark.parametrize("name", sorted(COMMENT_LINE_FLOODS))
def test_200kb_line_of_comments_parses_in_half_a_second(name, section):
    text = review("No findings.", section + [COMMENT_LINE_FLOODS[name]], ZERO,
                  low_heading=False)
    started = time.perf_counter()
    analysis = analyze(text)
    elapsed = time.perf_counter() - started
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert elapsed < 0.5, f"{name}: {elapsed:.2f}s"


def test_only_the_comment_opening_a_line_may_run_across_blank_lines():
    """A later comment on a comment block's line must close inside its block,
    so it hides nothing below the blank line."""
    assert_blocks_behind_zero_footer(
        ["<!-- a --> <!--", "", "SQL injection `` ` `` severity: HIGH `x`",
         "", "-->"])


@pytest.mark.parametrize("body, severity", [
    # A severity row after a flood of empty cells.
    (["|" * 5000, "| S1 | HIGH | SQL injection |"], "HIGH"),
    # A tally header still applies to a later row with no severity word.
    (["| CRITICAL | HIGH |", "|---|---|", "| 0 | 1 |"], "HIGH"),
    (["| CRITICAL | HIGH |", "|---|---|", "|" * 5000, "| 2 | 0 |"],
     "CRITICAL"),
    # A severity cell inside the flood row itself.
    (["|" * 5000 + " S1 | HIGH | SQL injection |"], "HIGH"),
])
def test_table_rows_still_count_after_rows_without_a_severity(body, severity):
    assert_blocks_behind_zero_footer(body, severity)


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
