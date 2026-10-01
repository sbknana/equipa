"""Review-gate hardening from the 2026-09-29 review (tasks 3109 / 3113).

Every case runs the real gate code (``_security_review_blocks_merge``,
``verify_reviewer_provenance``, ``decide_merge_gate``, ``run_security_review``
and the review parser). Each one reproduced a bypass or an honest-review
false block on main before this work:

* gate-04: build/dependency files and agent instruction files counted as
  doc-only and skipped the security review.
* gate-06 / loop-11: a HIGH written as a table row, a ``Severity:`` field, a
  setext or HTML heading, "High-severity", a "(HIGH)" list item, in
  fullwidth letters or split by a zero-width character merged behind an
  all-zero footer.
* gate-07: an unfinished review (no completion sentinel, Draft summary,
  reviewer that ran out of turns) was trusted.
* gate-09 / gate-14: CRLF, lone CR, U+2028 and U+2029 line breaks broke the
  nonce check for honest reviews and hid headings from the parser.

The source stays ASCII: special characters are written as escapes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from equipa import agent_runner, loops
from equipa import security_gate as sg
from equipa.dispatch import _security_review_blocks_merge
from equipa.security_gate import (
    REVIEWER_STATUS_SUCCEEDED,
    ReviewerRunRecord,
    fingerprint_artifact,
    get_reviewer_run,
    is_doc_only_diff,
    record_reviewer_run,
    review_complete_line,
    reviewer_nonce_line,
    reviewer_run_failure,
    verify_reviewer_provenance,
)

NONCE = "0123456789abcdef0123456789abcdef"
OTHER_NONCE = "fedcba9876543210fedcba9876543210"

ZERO_FOOTER = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0"
ONE_LOW_FOOTER = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0"
ONE_HIGH_FOOTER = "CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0"

LINE_SEPARATOR = " "
PARAGRAPH_SEPARATOR = " "
FULLWIDTH_HIGH = "ＨＩＧＨ"
ZERO_WIDTH_SPACE_HIGH = "HI​GH"
SOFT_HYPHEN_HIGH = "H­IGH"


def review_body(
    finding_heading: str = "LOW",
    footer: str = ONE_LOW_FOOTER,
    summary: str = "1 finding.",
    newline: str = "\n",
    nonce: str = NONCE,
    tail: list[str] | None = None,
) -> str:
    """A reviewer artifact with one finding heading and a Counts footer."""
    lines = [
        reviewer_nonce_line(nonce), "# Security Review", "",
        "## Summary", summary, "",
        f"### [E1] {finding_heading} - verbose error message", "Details.", "",
        "## Counts", footer,
    ]
    if tail is not None:
        lines += tail
    return newline.join(lines) + newline


def finished_review(**kwargs: object) -> str:
    """``review_body`` ending with the completion sentinel, as a real reviewer must."""
    nonce = kwargs.get("nonce", NONCE)
    return review_body(tail=[review_complete_line(nonce)], **kwargs)


def write_recorded_review(project_dir: Path, task_id: int, text: str) -> Path:
    """Write the artifact and record a succeeded reviewer run that produced it."""
    path = project_dir / ".equipa-artifacts" / f"SECURITY-REVIEW-{task_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    record_reviewer_run(ReviewerRunRecord(
        task_id=task_id, nonce=NONCE, status=REVIEWER_STATUS_SUCCEEDED,
        started_at=1.0, post_artifact=fingerprint_artifact(path), attempts=1,
    ))
    return path


def gate_result(project_dir: Path, task_id: int):
    return _security_review_blocks_merge(
        str(project_dir), task_id, block_on_missing=True,
    )


# Task 3143: a standalone UPPER-case severity word, as the backstop reads it.
SEVERITY_TOKEN = re.compile(r"(?<![^\W_])(CRITICAL|HIGH|MEDIUM)(?![^\W_])")


def lowercase_severity_words(text: str) -> str:
    """``text`` with CRITICAL, HIGH and MEDIUM in lower case, except in the
    final footer (written last, after every other line)."""
    body, separator, footer = text.rpartition("## Counts")
    if not separator:
        return SEVERITY_TOKEN.sub(lambda match: match.group(1).lower(), text)
    return SEVERITY_TOKEN.sub(
        lambda match: match.group(1).lower(), body,
    ) + separator + footer


def assert_only_the_backstop_blocks(text: str) -> None:
    """The rules trusted ``text`` (the backstop runs only then) and the
    severity-token backstop blocked it."""
    analysis = loops._analyze_review_file(
        Path("SECURITY-REVIEW-94300.md"), text=text)
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith("unaccounted CRITICAL/HIGH token at line "), (
        analysis.detail)


# --------------------------------------------------------------- gate-04

@pytest.mark.parametrize("path", [
    "requirements.txt", "requirements-dev.txt", "constraints.txt",
    "CMakeLists.txt", "sub/CMakeLists.txt", "prompts/security-reviewer.md",
    "standing_orders/x.md", "skills/security/foo/SKILL.md", "a/b/SKILL.md",
    "GEMINI.md", ".github/copilot-instructions.md", ".github/workflows/x.md",
    ".gitattributes", ".gitmodules", "CLAUDE.md", "AGENTS.md",
    "docs/requirements.txt", "docs/CMakeLists.txt", "notes.txt", "x.md",
    "docs/../prompts/x.md",
])
def test_build_and_instruction_files_are_not_doc_only(path):
    assert is_doc_only_diff([path]) is False
    assert is_doc_only_diff(["README.md", path]) is False


@pytest.mark.parametrize("files", [
    ["docs/guide.md"], ["README.md"], ["README"], ["CHANGELOG.md"],
    ["LICENSE"], ["LICENSE.txt"], ["docs/a/b.rst", "README.rst"],
    ["docs/notes.txt", "README.md", "CHANGELOG.md"],
])
def test_real_documentation_is_still_doc_only(files):
    assert is_doc_only_diff(files) is True


def test_decide_merge_gate_blocks_requirements_and_prompt_changes(tmp_path):
    requirements = sg.decide_merge_gate(
        ["requirements.txt"],
        security_review_blocks_merge=lambda *a, **k: (True, None),
        project_dir=str(tmp_path), task_id=91001,
    )
    assert requirements.blocks_merge is True
    prompt = sg.decide_merge_gate(
        ["prompts/security-reviewer.md"],
        security_review_blocks_merge=lambda *a, **k: (False, {}),
        project_dir=str(tmp_path), task_id=91002,
    )
    assert prompt.blocks_merge is True, prompt


def test_decide_merge_gate_merges_documentation(tmp_path):
    decision = sg.decide_merge_gate(
        ["docs/guide.md", "README.md"],
        security_review_blocks_merge=lambda *a, **k: (True, None),
        project_dir=str(tmp_path), task_id=91003,
    )
    assert decision.blocks_merge is False


# ------------------------------------------------ gate-07 completion sentinel

def test_honest_review_with_sentinel_merges(tmp_path):
    write_recorded_review(tmp_path, 92001, finished_review())
    artifact = loops.find_review_artifact(str(tmp_path), "SECURITY-REVIEW", 92001)
    verdict = verify_reviewer_provenance(92001, artifact)
    assert verdict.trusted, verdict.reason
    assert gate_result(tmp_path, 92001)[0] is False


def test_trailing_blank_lines_after_sentinel_merge(tmp_path):
    text = review_body(tail=[review_complete_line(NONCE), "", "  "])
    write_recorded_review(tmp_path, 92008, text)
    assert gate_result(tmp_path, 92008)[0] is False


def test_missing_sentinel_blocks(tmp_path):
    write_recorded_review(tmp_path, 92002, review_body())
    assert gate_result(tmp_path, 92002) == (True, None)


def test_wrong_nonce_sentinel_blocks(tmp_path):
    text = review_body(tail=[review_complete_line(OTHER_NONCE)])
    write_recorded_review(tmp_path, 92003, text)
    assert gate_result(tmp_path, 92003) == (True, None)


def test_text_after_sentinel_blocks(tmp_path):
    text = review_body(tail=[review_complete_line(NONCE), "more text"])
    write_recorded_review(tmp_path, 92004, text)
    assert gate_result(tmp_path, 92004) == (True, None)


def test_draft_summary_without_sentinel_blocks(tmp_path):
    text = review_body(
        summary="Draft. Initial automated scan only; manual review pending",
    )
    write_recorded_review(tmp_path, 92005, text)
    assert gate_result(tmp_path, 92005) == (True, None)


def test_sentinel_only_mid_file_blocks(tmp_path):
    text = review_body().replace(
        "## Summary", review_complete_line(NONCE) + "\n## Summary",
    )
    write_recorded_review(tmp_path, 92006, text)
    assert gate_result(tmp_path, 92006) == (True, None)


def test_sentinel_with_upper_case_nonce_blocks(tmp_path):
    text = review_body(tail=[review_complete_line(NONCE.upper())])
    write_recorded_review(tmp_path, 92007, text)
    assert gate_result(tmp_path, 92007) == (True, None)


# ------------------------------------------------ gate-09 / gate-14 newlines

@pytest.mark.parametrize(
    "task_id, newline",
    [(93001, "\r\n"), (93002, "\r"), (93003, LINE_SEPARATOR),
     (93004, PARAGRAPH_SEPARATOR)],
    ids=["crlf", "cr", "u2028", "u2029"],
)
def test_honest_review_with_any_line_break_merges(tmp_path, task_id, newline):
    write_recorded_review(tmp_path, task_id, finished_review(newline=newline))
    blocks, counts = gate_result(tmp_path, task_id)
    assert blocks is False and counts and counts["LOW"] == 1, (blocks, counts)


@pytest.mark.parametrize(
    "task_id, newline",
    [(93101, "\r"), (93102, LINE_SEPARATOR), (93103, "\r\n")],
    ids=["cr", "u2028", "crlf"],
)
def test_hidden_high_heading_with_zero_footer_blocks(tmp_path, task_id, newline):
    text = finished_review(
        finding_heading="HIGH", footer=ZERO_FOOTER, newline=newline,
    )
    write_recorded_review(tmp_path, task_id, text)
    assert gate_result(tmp_path, task_id)[0] is True


@pytest.mark.parametrize(
    "task_id, newline",
    [(93201, "\r"), (93202, LINE_SEPARATOR), (93203, "\r\n")],
    ids=["cr", "u2028", "crlf"],
)
def test_high_heading_after_any_line_break_is_counted(tmp_path, task_id, newline):
    text = finished_review(
        finding_heading="HIGH", footer=ONE_HIGH_FOOTER, newline=newline,
    )
    write_recorded_review(tmp_path, task_id, text)
    blocks, counts = gate_result(tmp_path, task_id)
    assert blocks is True and counts and counts["HIGH"] == 1, (blocks, counts)


@pytest.mark.parametrize(
    "newline", ["\r", LINE_SEPARATOR, PARAGRAPH_SEPARATOR],
    ids=["cr", "u2028", "u2029"],
)
def test_parser_normalises_text_it_reads_itself(tmp_path, newline):
    """gate-14 / SE-1: calling the parser without ``text=`` must not reopen it."""
    path = tmp_path / "SECURITY-REVIEW-93300.md"
    text = review_body(finding_heading="HIGH", footer=ZERO_FOOTER, newline=newline)
    path.write_bytes(text.encode("utf-8"))
    analysis = loops._analyze_review_file(path)
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert loops._count_findings_in_review_file(path) is None


# -------------------------------------- gate-06 / loop-11 severity forms

@pytest.mark.parametrize(
    "task_id, word",
    [(94001, FULLWIDTH_HIGH), (94002, ZERO_WIDTH_SPACE_HIGH),
     (94003, SOFT_HYPHEN_HIGH)],
    ids=["fullwidth", "zero-width-space", "soft-hyphen"],
)
def test_obfuscated_high_heading_with_zero_footer_blocks(tmp_path, task_id, word):
    text = finished_review(finding_heading=word, footer=ZERO_FOOTER)
    write_recorded_review(tmp_path, task_id, text)
    assert gate_result(tmp_path, task_id)[0] is True


# A HIGH finding written in each form the parser used to miss. Every one
# replaces the "Details." paragraph of a review whose only strict finding
# heading is LOW and whose footer says HIGH: 0.
HIDDEN_HIGH_FORMS = {
    "table-row": "| ID | Severity |\n|---|---|\n| S1 | HIGH |",
    "table-row-bold-cell": "| ID | Severity | Title |\n|---|---|---|\n"
                           "| S1 | **High** | SQL injection |",
    "severity-field-bold": "### Finding 2 - SQL injection\n**Severity:** HIGH",
    "severity-field-plain": "### Finding 2 - SQL injection\nSeverity: High",
    "severity-field-list": "- Severity: HIGH",
    "severity-field-no-separator": "### Finding 2 - SQL injection\n"
                                   "**Severity** HIGH",
    "severity-level-field": "- Severity level = High",
    "setext-heading": "[S2] HIGH - SQL injection\n-------------------------",
    "setext-heading-equals": "[S2] HIGH - SQL injection\n=====",
    "html-heading": "<h3>[S2] HIGH - SQL injection</h3>",
    "html-heading-nested-tag": "<h3 id=\"s2\">[S2] <b>HIGH</b> - SQLi</h3>",
    "high-severity-heading": "### [S2] High-severity SQL injection",
    "list-item-parenthesis": "1. SQL injection in login (HIGH)",
    "list-item-bracket": "- [HIGH] token written to the log",
}


@pytest.mark.parametrize(
    "form", list(HIDDEN_HIGH_FORMS), ids=list(HIDDEN_HIGH_FORMS),
)
def test_hidden_high_form_with_zero_high_footer_fails_closed(tmp_path, form):
    text = finished_review().replace("Details.", HIDDEN_HIGH_FORMS[form])
    task_id = 94100 + list(HIDDEN_HIGH_FORMS).index(form)
    write_recorded_review(tmp_path, task_id, text)
    assert gate_result(tmp_path, task_id) == (True, None)
    analysis = loops._analyze_review_file(
        tmp_path / ".equipa-artifacts" / f"SECURITY-REVIEW-{task_id}.md",
    )
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis


@pytest.mark.parametrize(
    "form", list(HIDDEN_HIGH_FORMS), ids=list(HIDDEN_HIGH_FORMS),
)
def test_hidden_high_form_counted_in_footer_blocks_the_merge(tmp_path, form):
    """An honest review whose only finding uses the form, and counts it,
    blocks the merge: untrusted when the form holds an UPPER-case HIGH,
    otherwise trusted with HIGH=1 (task 3154, I3152-04: named for what it
    asserts).

    The strict ``### [E1] LOW`` header is removed: headers that disagree with
    the footer are a count mismatch on their own (task #3033), whatever else
    the review holds.
    """
    strict_finding = "### [E1] LOW - verbose error message\nDetails."
    text = finished_review(footer=ONE_HIGH_FOOTER)
    assert strict_finding in text
    text = text.replace(strict_finding, HIDDEN_HIGH_FORMS[form])
    task_id = 94200 + list(HIDDEN_HIGH_FORMS).index(form)
    write_recorded_review(tmp_path, task_id, text)
    blocks, counts = gate_result(tmp_path, task_id)
    if SEVERITY_TOKEN.search(HIDDEN_HIGH_FORMS[form]):
        # Task 3152: an UPPER-case HIGH that is not a counted heading's label
        # blocks even when the footer counts it (no section or footer-count
        # exemption); the merge was blocked before too, by HIGH=1.
        assert (blocks, counts) == (True, None)
    else:
        assert blocks is True and counts and counts["HIGH"] == 1, (
            blocks, counts)


# Lines that mention a severity word but report no finding. A clean review
# containing them must still merge.
BENIGN_SEVERITY_MENTIONS = {
    "tally-table": "| Severity | Count |\n|---|---|\n| HIGH | 0 |\n| LOW | 1 |",
    "column-tally": "| CRITICAL | HIGH | MEDIUM | LOW | INFO |\n"
                    "|---|---|---|---|---|\n| 0 | 0 | 0 | 1 | 0 |",
    "overall-risk-row": "| Overall risk | LOW |",
    "prose-high-level": "A high-level design note, not a finding.",
    "description-cell": "| Area | Note |\n|---|---|\n| cache | High memory use |",
    "list-item-low-risk-prose": "- Rotating keys is (low risk) to defer.",
    "thematic-break": "Details.\n\n---",
    "severity-ratings-prose": "Severity ratings follow CVSS: HIGH means 7.0+.",
    "clean-summary-prose": "No CRITICAL or HIGH findings in this area.",
    "html-comment": "<!-- reviewer note: HIGH bar for evidence -->",
}
@pytest.mark.parametrize(
    "mention", list(BENIGN_SEVERITY_MENTIONS),
    ids=list(BENIGN_SEVERITY_MENTIONS),
)
def test_benign_severity_mentions_do_not_block_a_clean_review(tmp_path, mention):
    """Task 3143: the reviewer prompt allows UPPER-case CRITICAL, HIGH and
    MEDIUM only as a finding's label, so a compliant mention is in lower case
    and merges. In UPPER case every rule still reads it as no finding (the
    review is trusted) and only the severity-token backstop blocks it.

    Task 3152: "tally-table" and "clean-summary-prose" (a zero tally and a
    negation) merged as written under the 3143 exemptions; CRITICAL and HIGH
    have none now, so they block like every other UPPER-case mention."""
    mention_text = BENIGN_SEVERITY_MENTIONS[mention]
    if SEVERITY_TOKEN.search(mention_text):
        assert_only_the_backstop_blocks(
            finished_review().replace("Details.", mention_text))
    text = finished_review().replace(
        "Details.", lowercase_severity_words(mention_text),
    )
    task_id = 94300 + list(BENIGN_SEVERITY_MENTIONS).index(mention)
    write_recorded_review(tmp_path, task_id, text)
    blocks, counts = gate_result(tmp_path, task_id)
    assert blocks is False and counts and counts["LOW"] == 1, (blocks, counts)


@pytest.mark.parametrize("line", [
    "Severity" + " " * 50000 + "x",
    "- Severity" + " " * 50000 + "level",
    "- x (" + " " * 50000 + "HIGH" + " " * 50000 + "x",
], ids=["severity-spaces", "severity-level-spaces", "list-paren-spaces"])
def test_new_severity_forms_parse_in_linear_time(tmp_path, line):
    """Adjacent unbounded ``[ \\t]*`` made one padded line take seconds."""
    import time

    text = review_body(footer=ONE_LOW_FOOTER).replace("Details.", line)
    path = tmp_path / "SECURITY-REVIEW-94400.md"
    path.write_text(text, encoding="utf-8")
    start = time.perf_counter()
    analysis = loops._analyze_review_file(path, text=text)
    assert time.perf_counter() - start < 2.0
    if SEVERITY_TOKEN.search(line):
        # Task 3143: an UPPER-case HIGH in prose is blocked by the backstop
        # only; the rules still read the padded line as prose.
        assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
        assert analysis.detail.startswith("unaccounted CRITICAL/HIGH token at line ")
        text = lowercase_severity_words(text)
        start = time.perf_counter()
        analysis = loops._analyze_review_file(path, text=text)
        assert time.perf_counter() - start < 2.0
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis


# --------------------------- gate-07 reviewer end to end (run_security_review)

@pytest.fixture
def fake_reviewer(monkeypatch):
    """Replace the reviewer CLI with scripted behaviours, one per attempt."""
    harness = SimpleNamespace(behaviours=[], calls=0, task_id=None)

    async def fake_run_agent(_cmd, timeout=None):
        harness.calls += 1
        nonce = get_reviewer_run(harness.task_id).nonce
        return harness.behaviours[harness.calls - 1](harness.task_id, nonce)

    @contextlib.contextmanager
    def fake_cli(*_args, **_kwargs):
        yield ["claude"]

    async def fake_diff(_project_dir):
        return 0

    monkeypatch.setattr(loops, "run_agent", fake_run_agent)
    monkeypatch.setattr(loops, "build_cli_command", fake_cli)
    monkeypatch.setattr(loops, "build_system_prompt", lambda *a, **kw: "prompt")
    monkeypatch.setattr(loops, "get_role_turns", lambda *a, **kw: 5)
    monkeypatch.setattr(loops, "get_role_model", lambda *a, **kw: "opus")
    monkeypatch.setattr(
        loops, "load_dispatch_config", lambda _p: {"security_review_timeout": 30},
    )
    monkeypatch.setattr(loops, "_measure_review_diff_lines", fake_diff)
    monkeypatch.setattr(loops, "_extract_security_findings", lambda _t: [])
    return harness


def run_review(task_id, worktree, stable=None, output=None):
    task = {"id": task_id, "title": "t", "description": "d", "project_id": 1}
    return asyncio.run(loops.run_security_review(
        task, str(worktree), {}, SimpleNamespace(dispatch_config=None),
        output=output if output is not None else [],
        stable_project_dir=str(stable) if stable else None,
    ))


def review_writer(root, newline="\n", extra=None):
    """A reviewer attempt that writes a finished review with its own nonce."""
    def behave(task_id, nonce):
        path = root / ".equipa-artifacts" / f"SECURITY-REVIEW-{task_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            finished_review(nonce=nonce, newline=newline).encode("utf-8"),
        )
        result = {"success": True, "result_text": "done", "errors": []}
        if extra:
            result.update(extra)
        return result
    return behave


# The agent env is allowlisted (loop-03), so the fake finds result.json next
# to itself rather than through an inherited variable.
FAKE_CLI_PRINTING_RESULT = (
    "import os, sys\n"
    "here = os.path.dirname(os.path.abspath(__file__))\n"
    "sys.stdout.write(open(os.path.join(here, 'result.json'),"
    " encoding='utf-8').read())\n"
)


@pytest.mark.parametrize(
    "subtype, expect_max_turns",
    [("error_max_turns", True), ("success", False)],
)
def test_run_agent_flags_a_run_that_hit_max_turns(
    tmp_path, monkeypatch, subtype, expect_max_turns,
):
    """The CLI's ``error_max_turns`` result is reported as ``hit_max_turns``.

    The security reviewer reads the flag and treats the run as failed
    (gate-07). Since task 3134 (F8) the run is also not a success, as on the
    streaming path: it was cut off, not finished.
    """
    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps({
        "type": "result", "subtype": subtype, "num_turns": 5,
        "result": "partial report written",
    }), encoding="utf-8")
    fake_cli = tmp_path / "fake_claude.py"
    fake_cli.write_text(FAKE_CLI_PRINTING_RESULT, encoding="utf-8")

    result = asyncio.run(agent_runner.run_agent(
        [sys.executable, str(fake_cli)], timeout=30, max_retries=0,
    ))

    assert result["success"] is (not expect_max_turns)
    assert bool(result.get("hit_max_turns")) is expect_max_turns
    assert ("Agent hit max turns limit" in result["errors"]) is expect_max_turns


def test_max_turns_reviewer_blocks_and_is_not_retried(tmp_path, fake_reviewer):
    fake_reviewer.task_id = 95001
    fake_reviewer.behaviours = [
        review_writer(tmp_path, extra={
            "hit_max_turns": True, "errors": ["Agent hit max turns limit"],
        }),
        review_writer(tmp_path),
    ]
    run_review(95001, tmp_path)
    assert fake_reviewer.calls == 1
    assert reviewer_run_failure(95001) == "max-turns"
    assert gate_result(tmp_path, 95001) == (True, None)


def test_honest_reviewer_end_to_end_merges(tmp_path, fake_reviewer):
    fake_reviewer.task_id = 95002
    fake_reviewer.behaviours = [review_writer(tmp_path)]
    run_review(95002, tmp_path)
    assert gate_result(tmp_path, 95002)[0] is False


def test_crlf_reviewer_stable_copy_keeps_provenance(tmp_path, fake_reviewer):
    worktree, stable = tmp_path / "wt", tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()
    fake_reviewer.task_id = 95003
    fake_reviewer.behaviours = [review_writer(worktree, newline="\r\n")]
    output: list[str] = []
    run_review(95003, worktree, stable=stable, output=output)
    joined = "\n".join(map(str, output))
    assert "provenance=verified" in joined, joined[-2000:]
    assert gate_result(worktree, 95003)[0] is False
