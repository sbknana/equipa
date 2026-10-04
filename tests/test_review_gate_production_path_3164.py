"""Task 3164 (R3161-01): the review gate's production path parses the
review as written.

``verify_reviewer_provenance`` returned the artifact NORMALISED, and every
merge decision parsed that copy. Normalisation deletes invisible characters
and applies NFKC, so the backstop's separated reading (task 3154) and its
fold-before-NFKC copy (task 3143) never saw "Rated<U+3164>HIGH" or the
lunate-sigma C there: the raw-text tests blocked them while the gate merged
them with zero critical and zero high counted.

Every test here goes through the production gate code, never
``_analyze_review_file`` alone:

* ``_security_review_blocks_merge`` (the merge decision);
* ``_merge_task_branch`` (the defensive invariant);
* ``run_security_review`` (the review loop's own counts);
* ``verify_reviewer_provenance`` then ``_count_findings_in_review_file``
  with ``provenance.text``, the expression all three use, over the probe
  corpus.

The source stays ASCII: special characters are named.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import equipa.dispatch as dispatch
from equipa import loops
from equipa.dispatch import _merge_task_branch, _security_review_blocks_merge
from equipa.security_gate import (
    REVIEWER_STATUS_SUCCEEDED,
    ReviewerRunRecord,
    SecurityGateBypassError,
    fingerprint_artifact,
    normalize_review_text,
    record_reviewer_run,
    verify_reviewer_provenance,
)
from tests.test_gate_provenance_3063 import reviewer_harness  # noqa: F401
from tests.test_review_gate_no_exemptions_3152 import (
    CONTEXTS, NONCE, build_review,
)

REPO = Path(__file__).resolve().parent.parent
CORPUS_FIXTURE = REPO / "tests" / "fixtures" / "review_gate_probe_corpus_3157.json"
# The newest tree the fixture judged on the raw path (main before task 3161).
FIXTURE_TREE = "502975b"

HANGUL_FILLER = "\N{HANGUL FILLER}"
HALFWIDTH_HANGUL_FILLER = "\N{HALFWIDTH HANGUL FILLER}"
ZERO_WIDTH_SPACE = "\N{ZERO WIDTH SPACE}"
SOFT_HYPHEN = "\N{SOFT HYPHEN}"
SUPERSCRIPT_ONE = "\N{SUPERSCRIPT ONE}"
CIRCLED_CAPITAL_H = "\N{CIRCLED LATIN CAPITAL LETTER H}"
LUNATE_SIGMA = "\N{GREEK CAPITAL LUNATE SIGMA SYMBOL}"
BRAILLE_BLANK = "\N{BRAILLE PATTERN BLANK}"

TAIL = " remote code execution in upload."

# Every body of SECURITY-REVIEW-3161 R3161-01. Each merged with zero critical
# and zero high on the production chain while the raw-text tests blocked it.
REPORTED_BODIES = {
    "hangul-filler-high": f"Rated{HANGUL_FILLER}HIGH{TAIL}",
    "hangul-filler-critical": f"Rated{HANGUL_FILLER}CRITICAL: RCE in upload.",
    "halfwidth-filler-high": f"Rated{HALFWIDTH_HANGUL_FILLER}HIGH{TAIL}",
    "zero-width-space-high": f"Rated{ZERO_WIDTH_SPACE}HIGH{TAIL}",
    "soft-hyphen-high": f"Rated{SOFT_HYPHEN}HIGH{TAIL}",
    "superscript-one-high": f"Rated HIGH{SUPERSCRIPT_ONE} remote code execution.",
    "circled-capital-high": f"Rated{HANGUL_FILLER}{CIRCLED_CAPITAL_H}IGH{TAIL}",
    "lunate-sigma-critical": f"Rated {LUNATE_SIGMA}RITICAL{TAIL}",
}

# The same glue for every blocking severity word (task 3161: an unaccounted
# upper-case medium blocks too).
GLUE_CHARACTERS = {
    "hangul-filler": HANGUL_FILLER,
    "halfwidth-filler": HALFWIDTH_HANGUL_FILLER,
    "zero-width-space": ZERO_WIDTH_SPACE,
    "soft-hyphen": SOFT_HYPHEN,
}

TASK_ID = 3164


def write_recorded_review(project_dir: Path, task_id: int, text: str) -> Path:
    """Write the artifact and record the succeeded reviewer run that wrote it."""
    path = project_dir / ".equipa-artifacts" / f"SECURITY-REVIEW-{task_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    record_reviewer_run(ReviewerRunRecord(
        task_id=task_id, nonce=NONCE, status=REVIEWER_STATUS_SUCCEEDED,
        started_at=1.0, post_artifact=fingerprint_artifact(path), attempts=1,
    ))
    return path


def merge_decision(project_dir: Path, text: str) -> tuple[bool, dict | None]:
    """``_security_review_blocks_merge`` on ``text`` as this cycle's review."""
    write_recorded_review(project_dir, TASK_ID, text)
    return _security_review_blocks_merge(
        str(project_dir), TASK_ID, block_on_missing=True,
    )


def stop_before_git(monkeypatch) -> None:
    """End ``_merge_task_branch`` right after its invariant, before any git."""
    def untrusted(_project_dir):
        raise dispatch.UntrustedDefaultBranchError("test: stop before git")

    monkeypatch.setattr(dispatch, "get_trusted_default_branch", untrusted)


# --- What provenance hands the gate ----------------------------------------

def test_provenance_returns_the_review_as_written(tmp_path):
    text = build_review([REPORTED_BODIES["hangul-filler-high"]], "zero")
    path = write_recorded_review(tmp_path, TASK_ID, text)

    provenance = verify_reviewer_provenance(TASK_ID, path)

    assert (provenance.trusted, provenance.reason) == (True, "verified")
    assert provenance.text == text
    assert HANGUL_FILLER in provenance.text


def test_an_honest_review_with_other_line_breaks_still_merges(tmp_path):
    """gate-09 / gate-14: the nonce, the sentinel and the parser still read
    CRLF and U+2028 line breaks as line breaks now that the parser gets the
    text as written."""
    for line_break in ("\r\n", "\N{LINE SEPARATOR}"):
        text = build_review(["Input is validated."], "one_low")
        blocks, counts = merge_decision(
            tmp_path, text.replace("\n", line_break))
        assert blocks is False, line_break
        assert (counts["HIGH"], counts["LOW"]) == (0, 1), line_break


# --- The merge decision ----------------------------------------------------

@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("name", sorted(REPORTED_BODIES))
def test_a_reported_body_blocks_the_merge(tmp_path, capsys, name, context):
    text = build_review([REPORTED_BODIES[name]], context)

    assert merge_decision(tmp_path, text) == (True, None)
    assert "event=count-mismatch" in capsys.readouterr().err


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("severity", loops.MERGE_BLOCKING_SEVERITIES)
@pytest.mark.parametrize("glue", sorted(GLUE_CHARACTERS))
def test_a_glued_severity_word_blocks_the_merge(tmp_path, glue, severity,
                                                context):
    body = f"Rated{GLUE_CHARACTERS[glue]}{severity}{TAIL}"

    assert merge_decision(tmp_path, build_review([body], context)) == (
        True, None)


@pytest.mark.parametrize("body", [
    f"Rated HIGH{TAIL}",
    f"Rated{BRAILLE_BLANK}HIGH{TAIL}",
])
def test_a_visible_word_still_blocks_the_merge(tmp_path, body):
    """Controls: both paths blocked these before the fix."""
    assert merge_decision(tmp_path, build_review([body], "zero")) == (
        True, None)


@pytest.mark.parametrize("body", [
    f"Rated high{TAIL}",
    f"Rated{HANGUL_FILLER}high{TAIL}",
    f"Rated{ZERO_WIDTH_SPACE}critical: RCE in upload.",
    f"Rated high{SUPERSCRIPT_ONE} remote code execution.",
])
def test_lower_case_prose_still_merges(tmp_path, body):
    """Controls: the text as written blocks nothing the reviewer was allowed
    to write, whatever invisible character sits next to it."""
    blocks, counts = merge_decision(tmp_path, build_review([body], "zero"))

    assert blocks is False
    assert (counts["CRITICAL"], counts["HIGH"]) == (0, 0)


# --- The defensive invariant and the review loop ---------------------------

@pytest.mark.parametrize("name", sorted(REPORTED_BODIES))
def test_the_defensive_invariant_refuses_a_reported_body(
    tmp_path, capsys, monkeypatch, name,
):
    write_recorded_review(
        tmp_path, TASK_ID, build_review([REPORTED_BODIES[name]], "zero"))
    stop_before_git(monkeypatch)

    with pytest.raises(SecurityGateBypassError, match="missing or unparseable"):
        asyncio.run(_merge_task_branch(
            str(tmp_path), TASK_ID, f"forge-task-{TASK_ID}",
            expect_artifact=True,
        ))
    assert "reason=artifact-unparseable-or-missing" in capsys.readouterr().err


def test_the_defensive_invariant_passes_lower_case_prose(
    tmp_path, capsys, monkeypatch,
):
    """Control: the invariant lets the same review through in lower case;
    the merge then stops at the stubbed git step."""
    write_recorded_review(
        tmp_path, TASK_ID,
        build_review([f"Rated{HANGUL_FILLER}high{TAIL}"], "zero"))
    stop_before_git(monkeypatch)

    assert asyncio.run(_merge_task_branch(
        str(tmp_path), TASK_ID, f"forge-task-{TASK_ID}", expect_artifact=True,
    )) is False
    assert "event=defensive-invariant-fired" not in capsys.readouterr().err


def run_review_loop(harness, project_dir: Path, text: str) -> str:
    """One ``run_security_review`` cycle whose reviewer writes ``text`` with
    the run's own nonce; returns the loop's log."""
    def writes_review(task_id, nonce):
        path = (project_dir / ".equipa-artifacts"
                / f"SECURITY-REVIEW-{task_id}.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.replace(NONCE, nonce).encode("utf-8"))
        return {"success": True, "result_text": "done", "errors": []}

    harness.task_id = TASK_ID
    harness.behaviour = writes_review
    output: list[str] = []
    task = {"id": TASK_ID, "title": "t", "description": "d", "project_id": 1}
    asyncio.run(loops.run_security_review(
        task, str(project_dir), {}, SimpleNamespace(dispatch_config=None),
        output=output,
    ))
    return "\n".join(str(line) for line in output)


@pytest.mark.parametrize("name", ["hangul-filler-high",
                                  "lunate-sigma-critical"])
def test_the_review_loop_does_not_trust_a_reported_body(
    tmp_path, reviewer_harness, name,  # noqa: F811
):
    log = run_review_loop(
        reviewer_harness, tmp_path,
        build_review([REPORTED_BODIES[name]], "zero"))

    assert "not a trustworthy finished review" in log
    assert "No critical or high severity findings" not in log


def test_the_review_loop_trusts_lower_case_prose(
    tmp_path, reviewer_harness,  # noqa: F811
):
    log = run_review_loop(
        reviewer_harness, tmp_path,
        build_review([f"Rated{HANGUL_FILLER}high{TAIL}"], "zero"))

    assert "No critical or high severity findings" in log


# --- The probe corpus through the production chain -------------------------

# One text in REPLAY_STRIDE of each family, in both contexts, so the replay
# stays a few seconds; every family is represented.
REPLAY_STRIDE = 12


def _load_script(name: str):
    path = REPO / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_3164", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=1)
def _corpus_replay_texts() -> dict[str, list[tuple[str, str]]]:
    """Per family, (label, text) of the sampled probe texts that main blocked
    on the raw path and that normalisation changes (where the two paths can
    disagree)."""
    differential = _load_script("review_gate_differential")
    bodies = differential.probe_corpus_bodies()
    fixture = json.loads(CORPUS_FIXTURE.read_text(encoding="ascii"))
    assert fixture["bodies"] == len(bodies)
    blocked = {
        context: differential.decode_bits(
            fixture["blocked"][FIXTURE_TREE][context], len(bodies))
        for context in CONTEXTS
    }
    by_family: dict[str, list[tuple[int, str, list[str]]]] = {}
    for index, (key, _severity, lines) in enumerate(bodies):
        by_family.setdefault(key.split("|", 1)[0], []).append(
            (index, key, lines))
    replay: dict[str, list[tuple[str, str]]] = {}
    for family, rows in by_family.items():
        texts = []
        for index, key, lines in rows[::REPLAY_STRIDE]:
            for context in CONTEXTS:
                text = build_review(lines, context)
                if (blocked[context][index]
                        and normalize_review_text(text) != text):
                    texts.append((f"{key} {context}", text))
        replay[family] = texts
    return replay


def test_the_production_chain_blocks_the_probe_corpus(tmp_path):
    """Every sampled probe text main's raw path blocked is blocked by the
    provenance-then-count chain of the gate (before task 3164 about two
    thirds of them merged)."""
    path = tmp_path / ".equipa-artifacts" / f"SECURITY-REVIEW-{TASK_ID}.md"
    path.parent.mkdir(parents=True)
    replay = _corpus_replay_texts()
    merged = []
    judged = 0
    for family, texts in sorted(replay.items()):
        for label, text in texts:
            judged += 1
            path.write_bytes(text.encode("utf-8"))
            record_reviewer_run(ReviewerRunRecord(
                task_id=TASK_ID, nonce=NONCE,
                status=REVIEWER_STATUS_SUCCEEDED, started_at=1.0,
                post_artifact=fingerprint_artifact(path), attempts=1,
            ))
            provenance = verify_reviewer_provenance(TASK_ID, path)
            assert provenance.trusted, (label, provenance.reason)
            # task_id=None: the GATE-AUDIT line goes to stderr only, not to
            # one database row per probe text.
            counts = loops._count_findings_in_review_file(
                path, text=provenance.text)
            if counts is not None and counts["CRITICAL"] + counts["HIGH"] == 0:
                merged.append(f"{family}: {label}")
    assert not merged, (len(merged), merged[:10])
    # The tally family is ASCII and NFKC keeps private-use characters, so
    # normalisation changes none of their texts: both paths read them alike.
    assert {family for family, texts in replay.items() if texts} >= {
        "replay", "separator", "negation", "split", "noun", "comma", "list",
    }
    assert judged >= 3000
