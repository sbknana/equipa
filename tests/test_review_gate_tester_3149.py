#!/usr/bin/env python3
"""Task 3149, tester: independent checks of the 3143 backstop follow-ups.

The developer's rows live in test_review_gate_followups_3149.py. This file
adds what a tester found by probing the new code paths:

* R3143-03: the inline-markup view joins "[HI](x)GH" only when the link fits
  a bounded, paren-free, one-line shape. Longer link text or destinations,
  balanced or escaped parentheses, a title holding parentheses, a destination
  on the next line, shortcut and long-label reference links still render one
  UPPER-case word (checked with markdown-it-py, CommonMark preset) and merge
  behind an all-zero footer.
* R3143-07: a bidi override written as a numeric character reference
  ("&#x202E;HGIH&#x202C;") renders as a real override, so a reader sees HIGH;
  the byte-level reject never sees the control and the decoded view drops it.
* R3143-02: a generic noun after the token ("No HIGH issues: SQL injection
  ...") is accepted as a safe continuation even when a colon or dash and
  finding text follow it, so the label guard does not apply.
* R3143-06: the reviewer prompt keeps UPPER-case severity words only on the
  Counts template and the rule that explains UPPER case.
* Timing: 200 KB floods aimed at each new code path parse in under 0.5 s.

Copyright 2026 Forgeborn
"""

import functools
import re
from pathlib import Path

import pytest

from equipa import loops
from equipa.security_gate import review_complete_line, reviewer_nonce_line
from tests.host_timing import assert_linear_time
from tests.review_gate_production import AS_WRITTEN_ONLY_FINDINGS
from tests.review_gate_production import gate_blocks as production_gate_blocks
from tests.review_gate_production import production_seconds
from tests.review_gate_timing import median_cpu_seconds, timing_test

REPO = Path(__file__).resolve().parent.parent
NONCE = "fedcba9876543210fedcba9876543210"
ZERO = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0"
ONE_LOW = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0"
E = "\N{EM DASH}"
LOW_HEADING = f"### [E1] LOW {E} verbose error message"
RB = 200 * 1024


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
    """dispatch._security_review_blocks_merge itself (task 3167): untrusted,
    or CRITICAL/HIGH."""
    return production_gate_blocks(text, nonce=NONCE)


def zero_review(body):
    return review("No findings.", ["## Findings", ""] + list(body), ZERO)


def one_low_review(body):
    return review("1 finding.", ["## Notes", ""] + list(body), ONE_LOW,
                  heading=LOW_HEADING)


def assert_gate_blocks(body):
    for text in (zero_review(body), one_low_review(body)):
        assert gate_blocks(text), (body, analyze(text))


@pytest.mark.parametrize("finding", sorted(AS_WRITTEN_ONLY_FINDINGS))
def test_the_must_block_helper_reads_the_review_as_written(finding):
    """Task 3170 (IR67-02): a severity only the review as written shows
    blocks through this suite's helper, so a gate that parsed the normalised
    text (R3161-01) fails this suite too."""
    assert_gate_blocks([AS_WRITTEN_ONLY_FINDINGS[finding]])


# --- R3143-03: every Markdown link shape that joins a word -----------------------

# Each body renders "HIGH: SQL injection in login.py" in CommonMark (verified
# with markdown-it-py while writing this test). The link regex of the inline
# view is bounded (200 / 500 / 100 characters), paren-free and one-line, so
# none of these is joined and each merges on the branch under test.
R3143_03_LINK_BODIES = {
    "empty_link_text": ["HI[](x)GH: SQL injection in login.py"],
    "long_link_text": ["[" + "a " * 101 + "HI](#x)GH: SQL injection in login.py"],
    "long_destination": ["[HI](#" + "a" * 600 + ")GH: SQL injection in login.py"],
    "balanced_parens_in_destination": [
        "[HI](a(b)c)GH: SQL injection in login.py"],
    "escaped_paren_in_destination": [
        "[HI](a\\)b)GH: SQL injection in login.py"],
    "title_holding_parens": ['[HI](#x "a(b)")GH: SQL injection in login.py'],
    "destination_on_next_line": ["See [HI](", "#x)GH: SQL injection in login.py"],
    "shortcut_reference": ["[HI]GH: SQL injection in login.py", "", "[HI]: #x"],
    "long_reference_label": [
        "[HI][" + "r" * 120 + "]GH: SQL injection in login.py", "",
        "[" + "r" * 120 + "]: #x"],
    "long_image_alt": ["HI![" + "a" * 250 + "](x)GH: SQL injection in login.py"],
}


@pytest.mark.parametrize("name", sorted(R3143_03_LINK_BODIES))
def test_r3143_03_every_link_shape_that_joins_a_word_blocks(name):
    assert_gate_blocks(R3143_03_LINK_BODIES[name])


# --- R3143-07: bidi controls written as character references ---------------------

# A numeric reference to a bidi control decodes to the control itself in the
# rendered HTML, so "&#x202E;HGIH&#x202C;" shows HIGH exactly like the raw
# RLO probe of indep-3143, which the developer's tests already block.
R3143_07_ENTITY_BIDI_BODIES = {
    "rlo_hex": ["&#x202E;HGIH&#x202C;: SQL injection in login.py"],
    "rlo_decimal": ["&#8238;HGIH&#8236;: SQL injection in login.py"],
    "rli_hex": ["&#x2067;HGIH&#x2069;: SQL injection in login.py"],
    "rlo_no_semicolon": ["&#x202EHGIH&#x202C: SQL injection in login.py"],
}


@pytest.mark.parametrize("name", sorted(R3143_07_ENTITY_BIDI_BODIES))
def test_r3143_07_bidi_control_written_as_a_reference_blocks(name):
    assert_gate_blocks(R3143_07_ENTITY_BIDI_BODIES[name])


# --- R3143-02: the label guard after a generic noun -------------------------------

# The task: an UPPER-case CRITICAL/HIGH is never excused when a colon, dash or
# finding text follows it; when in doubt, block. A generic noun between the
# token and the colon leaves the label in place.
R3143_02_NOUN_TAIL_BODIES = [
    ["No HIGH issues: SQL injection in login.py lets anyone in."],
    [f"No HIGH findings {E} RCE in upload.py lets anyone run code."],
    ["0 HIGH issues: SQL injection in login.py lets anyone in."],
    ["Not CRITICAL because: RCE via upload handler in upload.py."],
]


@pytest.mark.parametrize("body", R3143_02_NOUN_TAIL_BODIES)
def test_r3143_02_generic_noun_does_not_excuse_a_label(body):
    assert_gate_blocks(body)


# --- Shapes the branch already handles (regression rows) --------------------------

@pytest.mark.parametrize("body", [
    ["H\N{CYRILLIC LETTER PALOCHKA}GH: SQL injection in login.py"],
    ["H\N{LATIN LETTER DENTAL CLICK}GH: SQL injection in login.py"],
    ["\N{CHEROKEE LETTER MI}IGH: SQL injection in login.py"],
    ["\N{LISU LETTER XA}IGH: SQL injection in login.py"],
    ["\N{COPTIC CAPITAL LETTER HATE}IGH: SQL injection in login.py"],
    ["[H](#a)IGH: SQL injection in login.py"],
    ["H[IGH](#a): SQL injection in login.py"],
    ["*[HI](#x)*GH: SQL injection in login.py"],
    ["H``IGH``: SQL injection in login.py"],
    ["[HI][]GH: SQL injection in login.py", "", "[HI]: #x"],
    ["[HI](<a(b>)GH: SQL injection in login.py"],
    ["Was it fixed? No", "HIGH: SQL injection in login.py"],
    ["The scan found no", "HIGH", "SQL injection remains in login.py."],
])
def test_branch_blocks_other_split_and_lookalike_labels(body):
    assert_gate_blocks(body)


@pytest.mark.parametrize("position", [
    "## Counts\nCRITICAL: 0 | H\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}GH: 0",
    "**H\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}GH**: SQL injection",
    "| S1 | H\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}GH/MEDIUM |",
    "CR\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}T"
    "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}CAL: 0 issues",
    "MED\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}UM: x",
])
def test_r3143_05_dotted_capital_i_never_raises(position):
    """U+0130 in footer, emphasis, table and tally positions returns a
    verdict (KeyError before task 3149) and never loosens the gate."""
    text = zero_review([position])
    analysis = analyze(text)
    assert analysis.verdict in (loops.REVIEW_VERDICT_OK,
                                loops.REVIEW_VERDICT_COUNT_MISMATCH), analysis
    if "H\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}GH**: SQL" in position:
        assert gate_blocks(text), analysis


def test_long_reference_inside_a_counted_review_keeps_its_counts():
    """A 5000-digit reference beside a real HIGH heading neither raises nor
    drops the counted HIGH (the merge stays blocked)."""
    text = review("1 finding.", [
        "## Findings", "", f"### [S1] HIGH {E} SQL injection in login.py",
        "See &#" + "9" * 5000 + "; in the log.",
    ], "CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0")
    analysis = analyze(text)
    assert analysis.trusted, analysis
    assert analysis.counts["HIGH"] == 1
    assert gate_blocks(text)


# --- R3143-06: the reviewer prompt's own prose is lower case ----------------------

def test_r3143_06_prompt_keeps_upper_case_only_where_it_labels_or_explains():
    prompt = (REPO / "prompts" / "security-reviewer.md").read_text(
        encoding="utf-8")
    upper = [
        (number, line) for number, line in enumerate(prompt.split("\n"), 1)
        if re.search(r"(?<![A-Za-z])(?:CRITICAL|HIGH|MEDIUM)(?![A-Za-z])", line)
    ]
    allowed = [
        line for _, line in upper
        if line.strip() == "CRITICAL: N | HIGH: N | MEDIUM: N | LOW: N | INFO: N"
        or line.startswith("- **Finding-shaped lines are counted as findings")
    ]
    assert len(allowed) == 2, upper
    assert len(upper) == 2, [number for number, _ in upper]


def test_r3143_06_prompt_keeps_every_obligation():
    """Lower-casing the prose must not drop a rule (task: keep every
    obligation)."""
    prompt = (REPO / "prompts" / "security-reviewer.md").read_text(
        encoding="utf-8")
    for obligation in (
        "log each high or critical finding as a decision in TheForge",
        "Log every high and critical finding to TheForge decisions table",
        "decision_type='security_finding'",
        "One heading per finding:",
        "The `## Counts` footer must agree with the finding headings.",
        "A finding heading still counts when it is marked fixed or resolved.",
        "Never paste a bidi control character",
    ):
        assert obligation in prompt, obligation
    for level in ("critical", "high", "medium", "low", "info"):
        assert f"- **{level}** \N{EM DASH}" in prompt, level


# --- Timing: 200 KB floods aimed at the 3149 code paths ---------------------------

def pad(rb, line):
    return [line] * (rb // (len(line.encode()) + 1) + 1)


@functools.lru_cache(maxsize=None)
def timing_families(rb):
    """The floods built at ``rb`` bytes: timed at RB and a quarter of it
    (task 3171)."""
    return {
        # Inline-markup view (R3143-03).
        "inline_split_tokens": ["H*IG*H " * (rb // 7)],
        "mark_runs_after_letter": pad(rb, "a" + "*" * 1000 + " "),
        "backslash_flood": ["\\" * rb],
        "backtick_flood": ["`" * rb],
        "letter_mark_alternation": ["a*" * (rb // 2)],
        "open_bracket_flood": ["[" * rb],
        "open_link_flood": ["[a](" * (rb // 4)],
        "open_image_flood": ["![](" * (rb // 4)],
        "max_bounded_links": pad(rb, "[" + "a" * 200 + "](" + "b" * 499 + " "),
        "reference_link_tokens": ["[HI][r]GH " * (rb // 10)],
        "collapsed_reference_pairs": ["[][]" * (rb // 4)],
        # Numeric references (R3143-04).
        "long_numeric_references": pad(rb, "&#" + "9" * 5000 + " "),
        "zero_padded_references": pad(rb, "&#" + "0" * 1000 + "72;IGH "),
        "hex_references_flood": ["&#xFFFFFFF;" * (rb // 11)],
        # U+0130 through the case-insensitive rules (R3143-05).
        "dotted_capital_i_tokens": ["H\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}GH "
                                    * (rb // 6)],
        "dotted_capital_i_labels": pad(rb, "- H\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}"
                                       "GH: x"),
        # Lookalike letters (R3143-07).
        "ascii_lookalike_tokens": ["HlGH CR1T1CAL MED|UM " * (rb // 21)],
        # TeX math view (R3143-07).
        "math_spelled_tokens": ["$\\mathrm{H}\\mathrm{I}\\mathrm{G}\\mathrm{H}$ "
                                * (rb // 37)],
        "dollar_flood": ["$" * rb],
        "unclosed_math_runs": pad(rb, "$" + "\\a{ " * 600),
        "math_gap_flood": ["$" + "\\, " * (rb // 3) + "$"],
        # Bidi (R3143-07).
        "bidi_override_flood":["\N{RIGHT-TO-LEFT OVERRIDE}" * (rb // 3)],
        "allowed_mark_flood": ["\N{LEFT-TO-RIGHT MARK}x" * (rb // 4)],
        "entity_bidi_flood": ["&#x202E;" * (rb // 8)],
        # Exemption guard and soft wraps (R3143-02, R3143-06).
        "soft_wrapped_negations": ["the scan found no", "HIGH issues."]
        * (rb // 31),
        # Each token runs the strict tally grammar over up to 200 characters
        # that fail only at the last one (0.13 s / 0.26 s on main).
        "tally_tails_that_fail": pad(rb, "HIGH: 0" + " 1" * 95 + " x"),
        "tally_separator_tails_that_fail": pad(rb, "HIGH: 0" + " |" * 95 + " x"),
        "cut_after_text": pad(rb, "No HIGH" + " " * 300),
        "generic_noun_tails": pad(rb, "No HIGH issues remain because the scan was"),
        "list_flood_with_commas": ["not LOW, " * (rb // 9)],
        # Uncounted heading-shaped lines (R3143-01; about 0.6 s on main too).
        # Over _REVIEW_MAX_SEVERITY_LINES these block as too dense to parse; the
        # "at_cap" families below hold just under the cap, padded to 200 KB.
        "uncounted_headings": pad(rb, f"<p>### [S2] HIGH {E} x</p>"),
        "entity_headings": pad(rb, f"&#35;## [S2] MEDIUM {E} x"),
        "at_cap_uncounted_headings": at_cap(
            rb, f"<p>### [S{{n}}] HIGH {E} x {{pad}}</p>"),
        "at_cap_entity_headings": at_cap(
            rb, f"&#35;## [S{{n}}] MEDIUM {E} x {{pad}}"),
        "at_cap_heading_labels": at_cap(rb, "<p>### [S{n}] HIGH: x {pad}</p>"),
        "at_cap_tag_split_words": at_cap(rb, "<b>H</b>IGH <i>x</i> {pad}"),
        "at_cap_lookalike_words": at_cap(
            rb, "\N{CYRILLIC CAPITAL LETTER EN WITH DESCENDER}IGH {pad}"),
        "at_cap_list_labels": at_cap(rb, "- [S{n}] HIGH: x {pad}"),
    }


def at_cap(rb, template):
    """``rb`` bytes in just under the cap's number of lines holding a
    severity word, each padded with a run the line rules must read to its
    end. The line count stays at the cap whatever the size, so a quarter of
    the size shortens each run."""
    count = loops._REVIEW_MAX_SEVERITY_LINES - 20
    unpadded = sum(len(template.format(n=number, pad="").encode()) + 1
                   for number in range(count))
    room = (rb - unpadded) // count + 1
    return [template.format(n=number, pad="y" * room)
            for number in range(count)]


TIMING_FAMILIES = timing_families(RB)
PROVENANCE_REFUSED_FAMILIES = {"bidi_override_flood", "entity_bidi_flood"}


def _flood_seconds(name, rb):
    """The merge gate's time on the review holding flood ``name`` built at
    ``rb`` bytes."""
    text = review("No findings.", timing_families(rb)[name], ZERO)
    assert len(text.encode()) >= rb * 0.99, name
    return production_seconds(text, nonce=NONCE)


def _flood_parser_seconds(name, rb):
    text = review("No findings.", timing_families(rb)[name], ZERO)
    return median_cpu_seconds(analyze, text)


@timing_test
@pytest.mark.parametrize("name", sorted(TIMING_FAMILIES))
def test_200kb_flood_on_a_3149_path_parses_in_half_a_second(name):
    """Budget host-calibrated, growth from 50 KB to 200 KB linear (task
    3171)."""
    assert_linear_time(lambda rb: _flood_seconds(name, rb), RB, 0.5, name)
    if name in PROVENANCE_REFUSED_FAMILIES:
        # Provenance refuses a bidi control before the parser runs, so the
        # parser's own reading of the flood is timed as well.
        assert_linear_time(lambda rb: _flood_parser_seconds(name, rb), RB,
                           0.5, f"{name} (parser)")
