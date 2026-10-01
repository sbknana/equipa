#!/usr/bin/env python3
"""Task 3143: fail-closed severity-token backstop for the review gate.

Fix-forward of task 3137, which the independent review rejected
(indep-3137, SECURITY-REVIEW-3137). Three rounds of rule-by-rule parser fixes
each left or opened a shape that renders as a finding and is not counted, so
the gate now ends with a backstop that models no shape: every standalone
UPPER-case CRITICAL, HIGH or MEDIUM anywhere in the review (code and quotes
included) must be a counted finding, or the review blocks with the reason
"unaccounted severity token" (task 3152: "unaccounted CRITICAL/HIGH token")
and the line numbers.

Also covered, each with rows that fail before this task:

* R3137-00: an HTML list item whose text opens with ">" merged after 3137.
* R3137-03: a marker comment between "## Counts" and its tally hid the footer.
* I-01: a browser closes an HTML comment at "--!>".
* I-02: raw HTML blocks (a tag alone on its line, "<pre>") make fences text.
* I-03: numeric references without ";" render as letters in HTML blocks.
* I-04: format characters missing from the invisible-character list.
* R3137-01, -02, -04: digit-led titles, backslash escapes, 4-space
  continuations, all now blocked by the backstop.
* R3137-05: UPPER-case prose before a capitalised word blocks by the new
  prompt rule; the same prose in lower case merges.
* Timing: every 200 KB adversarial family (the reviewer's 40 plus the
  backstop's own) parses in under 0.5 s.

Copyright 2026 Forgeborn
"""

import hashlib
import json
import random
import time
import unicodedata
from pathlib import Path

import pytest

from equipa import loops
from equipa.security_gate import (
    normalize_review_text,
    review_complete_line,
    reviewer_nonce_line,
)

REPO = Path(__file__).resolve().parent.parent
NONCE = "0123456789abcdef0123456789abcdef"
ZERO = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0"
ONE_LOW = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0"
ONE_MEDIUM = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 1 | LOW: 0 | INFO: 0"
E = "\N{EM DASH}"


def review(summary, body, footer, *, heading=None, footer_prefix=()):
    """A finished review: provenance line, Summary, body, Counts, sentinel."""
    lines = [reviewer_nonce_line(NONCE), "# Security Review", "",
             "## Summary", summary, ""]
    if heading:
        lines += [heading, "Details.", ""]
    lines += list(body) + ["", "## Files Reviewed", "- app.py", "",
                           "## Methodology", "Read the diff, ran semgrep.", "",
                           "## Counts", *footer_prefix, footer,
                           review_complete_line(NONCE)]
    return "\n".join(lines) + "\n"


def analyze(text):
    return loops._analyze_review_file(Path("SECURITY-REVIEW-1.md"), text=text)


def rules_only(text):
    """The shape rules without the backstop (what main ran before 3143)."""
    return loops._analyze_review_views(normalize_review_text(text))


def blocked_by_backstop(text):
    analysis = analyze(text)
    return (analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH
            and analysis.detail.startswith(loops.BACKSTOP_REASON))


def zero_review(body):
    return review("No findings.", ["## Findings", ""] + list(body), ZERO)


def assert_blocks(body):
    analysis = analyze(zero_review(body))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, (
        body, analysis.detail)


def assert_merges(body, footer=ONE_LOW,
                  heading=f"### [E1] LOW {E} verbose error message"):
    analysis = analyze(review("1 finding.", ["## Notes", ""] + list(body),
                              footer, heading=heading))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, (body, analysis.detail)


# --- A. The backstop -------------------------------------------------------------

# Every one of these renders a severity word somewhere a reader sees it, and
# the shape rules count none of them (rules_only trusts each review).
BACKSTOP_ONLY_BODIES = [
    ["The parser change was reviewed; HIGH risk remains in the cache."],
    ["```", "HIGH: SQL injection in login", "```"],
    ["Run `HIGH` to see it."],
    ["HIGH: 2FA bypass in login"],                      # R3137-01
    ["CRITICAL: 0-click RCE in upload"],                # R3137-01
    ["[HIGH] 2FA bypass"],                              # R3137-01
    ["Severity\\: HIGH"],                               # R3137-02
    ["\\[HIGH\\] SQL injection"],                       # R3137-02
    ["Finding 1 is below.", "    HIGH: SQL injection in login"],  # R3137-04
    ["<div>", "    HIGH: SQL injection in login", "</div>"],      # R3137-04
    ["Notes: _HIGH_ for the cache."],
    ["Notes: HI\N{ZERO WIDTH SPACE}GH for the cache."],
    ["Notes: \N{GREEK CAPITAL LETTER ETA}IGH for the cache."],
    ["Notes: \N{LATIN LETTER SMALL CAPITAL H}\N{LATIN LETTER SMALL CAPITAL I}"
     "\N{LATIN LETTER SMALL CAPITAL G}\N{LATIN LETTER SMALL CAPITAL H} cache."],
    ["Notes: \N{COPTIC CAPITAL LETTER HATE}IGH for the cache."],
    ["Notes: " + "".join(chr(0x1F170 + ord(letter) - ord("A"))
                         for letter in "HIGH") + " for the cache."],
    ["Notes: \N{GREEK CAPITAL LUNATE SIGMA SYMBOL}RITICAL for the cache."],
    ["Notes: H\N{COMBINING LOW LINE}IGH for the cache."],
    ["Notes: HI\U0001D173GH for the cache."],
    ["Notes: HI\U0001BCA0GH for the cache."],
    ["Notes: &#72IGH and more."],
    ["Notes: &#x48IGH and more."],
    ["Notes: HI<b>GH</b> and more."],
    ["Notes: HI<!--", "-->GH and more."],
    ["Notes: HI<!-- a --!>GH and more."],
    ["<!-- HIGH: SQL injection in login -->"],
    ["> > quoted: the HIGH one"],
]


@pytest.mark.parametrize("body", BACKSTOP_ONLY_BODIES)
def test_backstop_blocks_a_severity_word_the_rules_do_not_count(body):
    text = zero_review(body)
    assert blocked_by_backstop(text), (body, analyze(text).detail)


@pytest.mark.parametrize("body", [
    ["Notes: \N{GREEK CAPITAL LUNATE SIGMA SYMBOL}RITICAL for the cache."],
    ["Notes: HI<!-- a --!>GH and more."],
    ["Notes: _HIGH_ for the cache."],
    ["Notes: HI<b>GH</b> and more."],
])
def test_the_rules_alone_trusted_these_reviews(body):
    """These rows fail on main: the shape rules trusted each review."""
    assert rules_only(zero_review(body)).trusted, body


def test_the_reason_names_every_line():
    text = zero_review(["Line one mentions HIGH.", "", "Clean line.", "",
                        "And MEDIUM here, CRITICAL there."])
    analysis = analyze(text)
    lines = text.split("\n")
    expected = {severity: lines.index(next(line for line in lines
                                           if severity in line
                                           and "|" not in line)) + 1
                for severity in ("HIGH", "MEDIUM", "CRITICAL")}
    # Task 3152: the reason leads with the CRITICAL/HIGH lines.
    assert analysis.detail.startswith(
        f"unaccounted CRITICAL/HIGH token at line {expected['HIGH']}, "
        f"{expected['CRITICAL']}: "), analysis.detail
    for severity, line in expected.items():
        assert f"{severity}=1 at line {line} " in analysis.detail, (
            severity, analysis.detail)


def test_the_reason_is_logged_by_the_gate(monkeypatch, tmp_path):
    logged = []
    monkeypatch.setattr(loops, "_gate_audit_log",
                        lambda message, **_: logged.append(message))
    path = tmp_path / "SECURITY-REVIEW-1.md"
    text = zero_review(["The HIGH one."])
    assert loops._count_findings_in_review_file(path, text=text) is None
    assert any("unaccounted CRITICAL/HIGH token at line" in message
               and "HIGH=1 at line" in message
               for message in logged), logged


@pytest.mark.parametrize("body", [
    ["Nothing low or info: LOW and INFO are not read."],
    ["no high-severity issues; critical paths were checked."],
    ["High-level design is fine."],
])
def test_low_info_and_lower_case_words_merge(body):
    assert_merges(body)


def test_footer_labels_and_completion_line_are_exempt():
    text = review("No findings.", ["Nothing to report."], ZERO)
    assert analyze(text).verdict == loops.REVIEW_VERDICT_OK


def test_a_counted_finding_heading_and_its_section_merge():
    text = review("1 finding.", [
        "## Findings", "",
        f"### [S1] MEDIUM {E} Missing rate limit",
        "- **Severity:** MEDIUM",
        "- MEDIUM because the endpoint needs a session.",
        "",
        "## Summary table",
        "| ID | Severity | Title |",
        "|---|---|---|",
        "| S1 | MEDIUM | Missing rate limit |",
    ], ONE_MEDIUM)
    analysis = analyze(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert analysis.counts["MEDIUM"] == 1


def test_another_severity_inside_a_counted_section_blocks():
    text = review("1 finding.", [
        "## Findings", "",
        f"### [S1] MEDIUM {E} Missing rate limit",
        "Also a HIGH risk here: SQL injection in login, found while checking.",
    ], ONE_MEDIUM)
    assert rules_only(text).trusted
    assert blocked_by_backstop(text), analyze(text).detail


ONE_HIGH = "CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0"


def assert_medium_counted(text, medium):
    """Task 3149 (R3143-06): unaccounted MEDIUM tokens alone never untrust a
    review (MEDIUM never blocks a merge); they are counted and named."""
    analysis = analyze(text)
    assert analysis.trusted, analysis.detail
    assert analysis.detail.startswith(loops.BACKSTOP_ADVISORY_REASON), (
        analysis.detail)
    assert analysis.counts["MEDIUM"] == medium, analysis.counts
    assert analysis.counts["CRITICAL"] == analysis.counts["HIGH"] == 0


def test_a_severity_word_after_the_section_blocks():
    """The section ends at the next heading; a later token is another one.

    With HIGH the review blocks; with MEDIUM it is counted (task 3149).
    """
    for severity, footer in (("HIGH", ONE_HIGH), ("MEDIUM", ONE_MEDIUM)):
        text = review("1 finding.", [
            "## Findings", "",
            f"### [S1] {severity} {E} Missing rate limit",
            "Details.", "",
            "## Notes",
            f"A {severity} issue in the cache was also seen.",
        ], footer)
        if severity == "HIGH":
            assert blocked_by_backstop(text), analyze(text).detail
        else:
            assert_medium_counted(text, 2)


def test_more_tokens_than_the_footer_counts_blocks():
    """Footer at 1 with one heading and one more token elsewhere."""
    for severity, footer in (("HIGH", ONE_HIGH), ("MEDIUM", ONE_MEDIUM)):
        text = review("1 finding.", [
            f"### [S1] {severity} {E} Missing rate limit", "", "## Notes",
            f"Separately, {severity}: verbose stack traces in the API.",
        ], footer)
        if severity == "HIGH":
            assert blocked_by_backstop(text)
        else:
            assert_medium_counted(text, 2)


def test_footer_covering_heading_less_findings_merges():
    """Findings written as prose and counted by the footer are accounted."""
    text = review("2 findings.", [
        "## Findings", "",
        "1. Missing rate limit on /login (MEDIUM).",
        "2. Verbose errors on /api (MEDIUM).",
    ], "CRITICAL: 0 | HIGH: 0 | MEDIUM: 2 | LOW: 0 | INFO: 0")
    analysis = analyze(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail


@pytest.mark.parametrize("body", [
    ["Rated LOW, not MEDIUM, as it needs a local account."],
    ["Kept at LOW rather than MEDIUM."],
])
def test_tallies_and_negations_merge(body):
    """MEDIUM keeps its tally and negation exemptions (it never blocks)."""
    assert_merges(body)


@pytest.mark.parametrize("body", [
    ["Totals: 0 CRITICAL / 0 HIGH / 0 MEDIUM / 1 LOW / 0 INFO."],
    ["semgrep: CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 across the diff."],
    ["| Severity | Count |", "|---|---|", "| HIGH | 0 |", "| CRITICAL | 0 |"],
    ["**No CRITICAL or HIGH findings.**"],
    ["There are no CRITICAL, HIGH or MEDIUM issues."],
    ["None is CRITICAL or HIGH."],
    ["0 CRITICAL/HIGH results from semgrep."],
])
def test_critical_high_tallies_and_negations_block(body):
    """Task 3152: these merged under the 3143 exemptions. CRITICAL and HIGH
    now have none, so each blocks; the lower-case prose merges."""
    analysis = analyze(review("1 finding.", ["## Notes", ""] + body, ONE_LOW,
                              heading=f"### [E1] LOW {E} verbose error"))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, body
    assert analysis.detail.startswith(loops.BACKSTOP_REASON + " at line ")
    assert_merges([line.replace("CRITICAL", "critical").replace("HIGH", "high")
                   for line in body])


@pytest.mark.parametrize("body", [
    ["Totals: 1 HIGH."],                     # a count the footer does not cover
    ["semgrep: HIGH: 2 across the diff."],
    ["No HIGH: SQL injection in login."],    # a negation word before a label
    ["Two MEDIUM issues remain in the cache."],
    ["Two HIGH issues remain in the cache."],
    ["Overall risk: HIGH."],
])
def test_tallies_beyond_the_counts_block(body):
    """MEDIUM-only rows are counted instead (task 3149, R3143-06)."""
    if "MEDIUM" in body[0]:
        assert_medium_counted(zero_review(body), 1)  # one token, counted once
    else:
        assert blocked_by_backstop(zero_review(body)), body


def test_one_medium_tally_with_one_counted_medium_merges():
    text = review("1 finding.", [
        f"### [S1] MEDIUM {E} Missing rate limit", "", "## Notes",
        "Overall risk: MEDIUM; the one MEDIUM has a one-line fix.",
    ], ONE_MEDIUM)
    assert analyze(text).verdict == loops.REVIEW_VERDICT_OK, analyze(text).detail


def test_an_untrusted_review_is_returned_unchanged():
    """The backstop only adds blocks: a review the rules block stays so."""
    rng = random.Random(3143)
    pool = ["", "HIGH: SQL injection", "- SQL injection - HIGH", "Prose.",
            "```", "`HIGH`", "<!--", "-->", "No HIGH findings.",
            f"### [S1] HIGH {E} x", "0 HIGH", "| HIGH | 0 |"]
    for _ in range(300):
        body = [rng.choice(pool) for _ in range(rng.randint(1, 6))]
        text = zero_review(body)
        before = rules_only(text)
        after = analyze(text)
        if not before.trusted:
            assert after == before, body
        elif after.trusted:
            assert after == before, body


# --- B. R3137-00: HTML list items whose text opens with ">" --------------------------

R3137_00_SHAPES = [
    "<ul><li>> SQL injection - HIGH</li></ul>",
    "<ul><li>> SQL injection (HIGH)</li></ul>",
    "<ul><li>> SQL injection, HIGH</li></ul>",
    "<ul><li>> SQL injection: HIGH</li></ul>",
    "<ul><li>> SQL injection [HIGH]</li></ul>",
    f"<ul><li>> SQL injection {E} HIGH</li></ul>",
    "<ul><li>> > SQL injection - HIGH</li></ul>",
    "<li>> SQL injection - HIGH",
    "<p>- > SQL injection (HIGH)</p>",
    "Finding 1<br>1. > SQL injection - HIGH",
]


@pytest.mark.parametrize("line", R3137_00_SHAPES)
def test_r3137_00_shapes_block_in_the_rules_again(line):
    """Main (3137) trusted these with zero counts; base 98933d0 blocked."""
    analysis = rules_only(zero_review([line]))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, (
        line, analysis.detail)
    assert "HIGH=" in analysis.detail


@pytest.mark.parametrize("line", R3137_00_SHAPES)
def test_r3137_00_shapes_block_the_merge(line):
    assert_blocks([line])


R3137_00_WRAPPERS = ["<ul><li>{}</li></ul>", "<li>{}", "Finding 1<br>{}",
                     "<p>{}</p>", "<details><summary>{}</summary></details>"]
R3137_00_PREFIXES = ["> ", "> > ", "- > ", "1. > ", "+ > > ", "* > "]
R3137_00_TAILS = ["SQL injection - HIGH", "SQL injection (HIGH)",
                  "SQL injection, HIGH", "SQL injection: HIGH",
                  "SQL injection [HIGH]"]


def test_r3137_00_family_never_merges():
    """Every wrapper x container run ending in ">" x trailing severity."""
    merged = []
    for wrapper in R3137_00_WRAPPERS:
        for prefix in R3137_00_PREFIXES:
            for tail in R3137_00_TAILS:
                line = wrapper.format(prefix + tail)
                if analyze(zero_review([line])).trusted:
                    merged.append(line)
    assert merged == []


# --- C. R3137-03: a marker line between "## Counts" and its tally ---------------------

HIGH_TWO = "CRITICAL: 0 | HIGH: 2 | MEDIUM: 0 | LOW: 0 | INFO: 0"


@pytest.mark.parametrize("prefix", [["<!-- EQUIPA-X -->"], ["<!-- note -->"],
                                    ["<!-- EQUIPA-X -->", ""]])
def test_r3137_03_marker_in_the_footer_keeps_its_counts(prefix):
    text = review("Two findings.", ["Two findings are described in prose."],
                  HIGH_TWO, footer_prefix=prefix)
    analysis = analyze(text)
    assert analysis.counts is not None and analysis.counts["HIGH"] == 2, (
        prefix, analysis)


def test_r3137_03_marker_as_the_only_summary_line_is_parsed_rendered():
    text = review("<!-- EQUIPA-X -->", ["Plain prose."], ZERO)
    calls = []
    real = loops._analyze_review_text

    def counting(view, nonblank_lines):
        calls.append(view)
        return real(view, nonblank_lines)

    loops._analyze_review_text = counting
    try:
        analysis = analyze(text)
    finally:
        loops._analyze_review_text = real
    assert len(calls) == 2, "a mid-document marker must not take the shortcut"
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail


def test_edge_marker_lines_still_parse_once(monkeypatch):
    calls = []
    real = loops._analyze_review_text
    monkeypatch.setattr(loops, "_analyze_review_text",
                        lambda view, nonblank: calls.append(view)
                        or real(view, nonblank))
    analysis = analyze(review("No findings.", ["Plain prose."], ZERO))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK
    assert len(calls) == 1


# --- D. I-01 to I-04 ----------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "<!-- a<b --!>HIGH: SQL injection -->",
    "<!-- " + "x" * 250 + " --!>HIGH: SQL injection -->",
    "<!-- a<b --!>[HIGH] SQL injection -->",
    "<!-- a<b --!>HIGH SQL injection in login handler -->",
    "<!-- a<b --!>Risk: HIGH -->",
    "<p><!-- a<b --!>HIGH: SQL injection --></p>",
    "<ul><li><!-- a<b --!>HIGH: SQL injection --></li></ul>",
])
def test_i01_comment_closed_by_bang_blocks(line):
    assert_blocks([line])


def test_i01_comment_end_closes_at_bang():
    text = "<!-- a --!>HIGH -->"
    assert loops._comment_end(text, 0, len(text)) == text.index("HIGH")
    assert loops._comment_end("<!-- a -->x", 0, 11) == 10
    assert loops._comment_end("<!-- a --! b", 0, 12) == -1


def test_i01_rendered_view_shows_text_after_bang():
    rendered = loops._rendered_review_text(
        normalize_review_text("<!-- a --!>HIGH: SQL injection -->\n"))
    assert "HIGH: SQL injection" in rendered


@pytest.mark.parametrize("body", [
    ["<span>", "```", "HIGH: SQL injection", "```", "</span>"],
    ["<x-note>", "```", "HIGH: SQL injection", "```", "</x-note>"],
    ["</span>", "```", "HIGH: SQL injection", "```"],
    ["<pre>", "notes", "", "```", "HIGH: SQL injection", "```", "</pre>"],
    ["<pre>", "notes", "", "~~~", "HIGH: SQL injection", "~~~", "</pre>"],
])
def test_i02_fences_in_raw_html_blocks_block(body):
    assert_blocks(body)


@pytest.mark.parametrize("body", [
    ["<p>&#72IGH: SQL injection</p>"],
    ["<p>&#x48IGH: SQL injection</p>"],
    ["<div>", "&#72IGH: SQL injection", "</div>"],
])
def test_i03_references_without_semicolon_block(body):
    assert_blocks(body)


@pytest.mark.parametrize("char", [
    "\U0001D173", "\U0001D17A", "\U0001BCA0", "\U0001BCA3", "\U00013430",
    "⁥", "￰", "\U000E0080",
])
def test_i04_format_characters_are_stripped(char):
    assert normalize_review_text(f"HI{char}GH") == "HIGH"


def test_i04_every_format_character_is_stripped():
    missing = [hex(code_point) for code_point in range(0x20000)
               if unicodedata.category(chr(code_point)) == "Cf"
               and normalize_review_text(f"a{chr(code_point)}b") != "ab"]
    assert missing == []


@pytest.mark.parametrize("char", ["\U0001D173", "\U0001BCA0"])
def test_i04_split_word_blocks_in_the_rules(char):
    analysis = rules_only(zero_review([f"HI{char}GH: SQL injection"]))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH


# --- R3137-05: UPPER-case prose before a capitalised word -----------------------------

R3137_05_LINES = [
    "HIGH Level overview of the change.",
    "HIGH Water mark logic was reviewed.",
    "- HIGH Availability mode is unchanged.",
    "> CRITICAL Path code was not modified.",
    "CRITICAL Section handling looks right.",
    "- CRITICAL Infrastructure: none touched.",
    "MEDIUM Priority follow-ups are listed below.",
    "MEDIUM Article links were checked.",
    "The gate counts findings by severity; severity HIGH blocks merges.",
]


@pytest.mark.parametrize("line", R3137_05_LINES)
def test_r3137_05_upper_case_prose_blocks_by_the_prompt_rule(line):
    """Fail closed: the prompt reserves UPPER case for a finding's label."""
    assert_blocks([line])


@pytest.mark.parametrize("line", R3137_05_LINES)
def test_r3137_05_the_same_prose_in_lower_case_merges(line):
    lowered = line
    for severity in ("CRITICAL", "HIGH", "MEDIUM"):
        lowered = lowered.replace(severity, severity.lower())
    assert_merges([lowered])


# --- Prompt and manifest -------------------------------------------------------------

PROMPT = REPO / "prompts" / "security-reviewer.md"


def test_prompt_states_the_backstop_rule():
    text = PROMPT.read_text()
    assert "UPPER case ONLY when labelling an actual finding" in text
    assert "no high-severity issues" in text
    assert "inside code blocks" in text
    assert ("anywhere in the review that is not a counted finding BLOCKS the "
            "merge") in text
    assert ("- **Anything else that renders as a severity word may also block "
            "the merge.**") in text
    # The shape-by-shape sentence is gone.
    assert "with character references (`&#72;`) or combining marks" not in text


def test_skill_manifest_hash_matches_the_prompt():
    manifest = json.loads((REPO / "skill_manifest.json").read_text())
    digest = hashlib.sha256(PROMPT.read_bytes()).hexdigest()
    assert digest in json.dumps(manifest)


# --- E. Timing ------------------------------------------------------------------------

RB = 200 * 1024


def pad(line):
    return [line] * (RB // (len(line.encode()) + 1) + 1)


# The 40 families of the independent review of 3137 ...
REVIEWER_FAMILIES = {
    "nl_only": ["\n" * RB],
    "cr_only": ["\r" * RB],
    "u2028_only": ["\N{LINE SEPARATOR}" * (RB // 3)],
    "crlf_only": ["\r\n" * (RB // 2)],
    "ff_only": ["\x0c" * RB],
    "sp_nl": ["    \n" * (RB // 5)],
    "dash_nl": ["-\n" * (RB // 2)],
    "bt_nl": ["`\n" * (RB // 2)],
    "gt_nl": [">\n" * (RB // 2)],
    "high_nl": ["HIGH\n" * (RB // 5)],
    "esc_bt": ["\\`" * (RB // 2)],
    "bt_runs_inc": ["".join("`" * i + "x" for i in range(1, 640))],
    "cmt_open": ["<!--" * (RB // 4)],
    "cmt_open_nl": ["<!--\n" * (RB // 5)],
    "cmt_empty": ["<!-->" * (RB // 5)],
    "cmt_bang": ["<!-- a --!>" * (RB // 11)],
    "tag_open": ["<a" * (RB // 2)],
    "tag_long": [("<a" + "x" * 498) * (RB // 500)],
    "li_html": ["<li>> " * (RB // 6)],
    "fn_flood": ["[^1]: " * (RB // 6)],
    "fn_lines": pad("[^1]: [^2]: HIGH"),
    "list_markers": ["- " * (RB // 2) + "HIGH: x"],
    "ol_markers": ["1. " * (RB // 3) + "HIGH: x"],
    "quote_markers": ["> " * (RB // 2) + "HIGH: x"],
    "quote_lines_nested": pad("> " * 50 + "HIGH: x"),
    "fence_lines": pad("```"),
    "fence_li_lines": pad("- ```"),
    "div_lines": pad("<div>"),
    "pipe_rows": ["| a | b |", "|---|---|"] + pad("|" * 200),
    "pipe_hi_rows": ["| a | b |", "|---|---|"]
    + pad("| HIGH to | MEDIUM or | HIGH |"),
    "comb_flood": ["H" + "\N{COMBINING LOW LINE}" * (RB // 2)],
    "comb_mixed": ["a\N{COMBINING ACUTE ACCENT}" * (RB // 3)],
    "amp_flood": ["&#72;" * (RB // 5)],
    "amp_bad": ["&#x" * (RB // 3)],
    "nbsp_runs": ["Severity:" + "\N{NO-BREAK SPACE}" * (RB // 2) + "HIGH"],
    "sev_clause": [", severity HIGH" * (RB // 15)],
    "title_runs": ["HIGH SQL " * (RB // 9)],
    "indent_cont": ["Finding"] + pad("    HIGH: x"),
    "marker_lines": pad("<!-- EQUIPA-X -->"),
    "marker_inline": ["HI<!-- EQUIPA-X -->" * (RB // 19)],
}
# ... and floods aimed at the backstop. Most hide their words from the shape
# rules (code spans, tallies, negations), so the backstop runs on all of it.
BACKSTOP_FAMILIES = {
    "code_tokens": ["`HIGH` " * (RB // 7)],
    "code_token_lines": pad("`MEDIUM` `CRITICAL`"),
    "tally_flood": ["0 HIGH, " * (RB // 8)],
    "tally_after_flood": ["HIGH: 0 | " * (RB // 10)],
    "negation_flood": ["no HIGH or " * (RB // 11)],
    "list_join_flood": ["0 CRITICAL" + "/HIGH" * (RB // 5)],
    "overall_risk_flood": ["Overall risk: MEDIUM. " * (RB // 22)],
    "tag_flood": ["<b>" * (RB // 3)],
    "tag_split_tokens": ["HI<b>GH " * (RB // 8)],
    "multiline_tags": ["<a\nb>" * (RB // 5)],
    "comment_split_lines": ["HI<!--\n-->GH " * (RB // 13)],
    "bogus_comments": ["<!x><?y></ z>" * (RB // 13)],
    "entity_no_semicolon": ["&#72IGH " * (RB // 8)],
    "named_entity": ["&ampHIGH " * (RB // 9)],
    "entity_newlines": ["&#10;" * (RB // 5)],
    "fullwidth": ["ＨＩＧＨ " * (RB // 13)],
    "small_caps": ["ʜɪɢʜ " * (RB // 9)],
    "negative_squared": ["\U0001F177\U0001F178\U0001F176\U0001F177 "
                         * (RB // 17)],
    # More distinct code points than the backstop's translate table keeps,
    # surrogates skipped (they cannot be written as UTF-8).
    "distinct_code_points": ["".join(
        chr(code_point) for code_point in range(0x4E00, 0x4E00 + RB // 3 + 2048)
        if not 0xD800 <= code_point <= 0xDFFF
    )],
    "format_chars": ["H​I­G\U0001D173H " * (RB // 14)],
    "counts_headings": pad("## Counts"),
    "counts_tally_long": ["## Counts", "CRITICAL: 0 " * (RB // 12)],
    "finding_headings": pad(f"### [S1] MEDIUM {E} x MEDIUM"),
    "id_lines": [f"### [S{index}] MEDIUM {E} x" for index in range(2000)]
    + pad("S7 MEDIUM S9 MEDIUM"),
}


def _best_of_two(text):
    best = float("inf")
    for _ in range(2):
        started = time.perf_counter()
        analyze(text)
        best = min(best, time.perf_counter() - started)
    return best


@pytest.mark.parametrize("name", sorted({**REVIEWER_FAMILIES,
                                         **BACKSTOP_FAMILIES}))
def test_200kb_adversarial_review_parses_in_half_a_second(name):
    body = {**REVIEWER_FAMILIES, **BACKSTOP_FAMILIES}[name]
    text = review("No findings.", body, ZERO)
    assert len(text.encode()) >= RB * 0.99, name
    elapsed = _best_of_two(text)
    assert elapsed < 0.5, f"{name}: {elapsed:.2f}s"


def test_the_reviewer_timing_set_has_forty_families():
    assert len(REVIEWER_FAMILIES) == 40
