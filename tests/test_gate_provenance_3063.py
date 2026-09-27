"""Task #3063 (GATE-PROVENANCE-2) — residuals of the #3041 security review.

  * SR41-03: a gate evaluation with NO reviewer record for the task trusted
    whatever artifact sat on disk — the #3035 failure mode by another path.
    It must BLOCK (GATE-AUDIT event=reviewer-record-missing), whatever
    ``block_on_missing`` says, except on the explicit doc-only path.
  * SR41-01: a FIFO / device / symlink at the artifact path was read with a
    blocking, unbounded ``read_bytes`` — a FIFO hung the event loop.
  * SR41-02: provenance was verified on one read and the counts parsed from
    another, so a file swapped in between was counted under a trusted sha.
  * SR41-04: the nonce proves a reviewer process wrote the file, not which
    one. The audit lines must name the reviewer run id, model and prompt hash.

Every test here fails on 3923301 (the #3041 head).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import equipa.dispatch as dispatch
from equipa import loops
from equipa.dispatch import _merge_task_branch, _security_review_blocks_merge
from equipa.loops import (
    ARTIFACTS_DIR_NAME,
    _count_findings_in_review_file,
    _write_security_review_fallback,
    run_security_review,
)
from equipa.security_gate import (
    MAX_REVIEW_ARTIFACT_BYTES,
    REVIEWER_RECORD_MISSING_REASON,
    SecurityGateBypassError,
    decide_merge_gate,
    fingerprint_artifact,
    get_reviewer_run,
    reviewer_nonce_line,
    set_unrecorded_reviewer_runs_permitted,
    verify_reviewer_provenance,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _review_body(nonce: str | None, *, high: int = 0, low: int = 1) -> str:
    """A complete, parser-trusted review; nonce line first when given."""
    header = f"{reviewer_nonce_line(nonce)}\n" if nonce else ""
    findings = "".join(
        f"### [H{index}] HIGH — command injection {index}\nDetails.\n\n"
        for index in range(1, high + 1)
    ) + "".join(
        f"### [L{index}] LOW — verbose error message {index}\nDetails.\n\n"
        for index in range(1, low + 1)
    )
    return (
        f"{header}# Security Review\n\n"
        f"## Summary\n{high + low} finding(s).\n\n"
        f"{findings}"
        f"## Counts\n"
        f"CRITICAL: 0 | HIGH: {high} | MEDIUM: 0 | LOW: {low} | INFO: 0\n"
    )


def _artifact(project_dir: Path, task_id: int) -> Path:
    return project_dir / ARTIFACTS_DIR_NAME / f"SECURITY-REVIEW-{task_id}.md"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


@pytest.fixture
def require_reviewer_record():
    """Production semantics: no hermetic opt-in to artifact-only trust."""
    previous = set_unrecorded_reviewer_runs_permitted(False)
    yield
    set_unrecorded_reviewer_runs_permitted(previous)


@pytest.fixture
def reviewer_harness(monkeypatch):
    """Run run_security_review with the agent layer scripted.

    ``harness.behaviour(task_id, nonce) -> result`` plays the reviewer.
    """
    harness = SimpleNamespace(behaviour=None, task_id=None)

    async def fake_run_agent(_cmd, timeout=None):
        record = get_reviewer_run(harness.task_id)
        return harness.behaviour(harness.task_id, record.nonce)

    @contextlib.contextmanager
    def fake_cli(*_args, **_kwargs):
        yield ["claude"]

    async def no_diff(_project_dir):
        return 0

    monkeypatch.setattr(loops, "run_agent", fake_run_agent)
    monkeypatch.setattr(loops, "build_cli_command", fake_cli)
    monkeypatch.setattr(loops, "build_system_prompt", lambda *a, **kw: "prompt")
    monkeypatch.setattr(loops, "get_role_turns", lambda *a, **kw: 5)
    monkeypatch.setattr(loops, "get_role_model", lambda *a, **kw: "opus")
    monkeypatch.setattr(
        loops, "load_dispatch_config",
        lambda _p: {"security_review_timeout": 30},
    )
    monkeypatch.setattr(loops, "_measure_review_diff_lines", no_diff)
    monkeypatch.setattr(loops, "_extract_security_findings", lambda _t: [])
    return harness


def _run_reviewer(harness, project_dir: Path, task_id: int, *, high: int = 0):
    """Run one reviewer cycle that writes a nonce-stamped review."""
    def writes_review(tid, nonce):
        _write(_artifact(project_dir, tid), _review_body(nonce, high=high))
        return {"success": True, "result_text": "done", "errors": []}

    harness.task_id = task_id
    harness.behaviour = writes_review
    task = {"id": task_id, "title": "t", "description": "d", "project_id": 1}
    asyncio.run(run_security_review(
        task, str(project_dir), {}, SimpleNamespace(dispatch_config=None),
        output=[],
    ))


def _stop_before_git(monkeypatch):
    """Stop _merge_task_branch right after the invariant, before any git."""
    def untrusted(_project_dir):
        raise dispatch.UntrustedDefaultBranchError("test: stop before git")

    monkeypatch.setattr(dispatch, "get_trusted_default_branch", untrusted)


# ---------------------------------------------------------------------------
# SR41-03: no reviewer record for this cycle BLOCKS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("block_on_missing", [True, False])
def test_no_reviewer_record_blocks_regardless_of_block_on_missing(
    tmp_path, capsys, require_reviewer_record, block_on_missing,
):
    """A clean developer self-review with no reviewer run is the #3035 shape:
    it must block even in the legacy fail-open mode."""
    _write(_artifact(tmp_path, 3201), _review_body(None, low=2))

    blocks, counts = _security_review_blocks_merge(
        str(tmp_path), 3201, block_on_missing=block_on_missing,
    )

    assert (blocks, counts) == (True, None)
    err = capsys.readouterr().err
    assert "task=3201 event=reviewer-record-missing" in err
    assert f"provenance={REVIEWER_RECORD_MISSING_REASON}" in err


def test_no_reviewer_record_defensive_invariant_refuses_merge(
    tmp_path, capsys, monkeypatch, require_reviewer_record,
):
    _write(_artifact(tmp_path, 3202), _review_body(None, low=1))
    _stop_before_git(monkeypatch)

    with pytest.raises(SecurityGateBypassError, match="reviewer-record-missing"):
        asyncio.run(_merge_task_branch(
            str(tmp_path), 3202, "forge-task-3202", expect_artifact=True,
        ))
    assert "reason=reviewer-record-missing" in capsys.readouterr().err


def test_no_reviewer_record_doc_only_path_still_merges(
    tmp_path, capsys, monkeypatch, require_reviewer_record,
):
    """The explicit doc-only path expects no reviewer and no artifact."""
    def must_not_evaluate(*_args, **_kwargs):
        raise AssertionError("doc-only diff must not evaluate the artifact")

    decision = decide_merge_gate(
        ["README.md", "docs/guide.md"],
        security_review_blocks_merge=must_not_evaluate,
        project_dir=str(tmp_path),
        task_id=3203,
    )
    assert decision.blocks_merge is False
    assert decision.expect_artifact is False

    _stop_before_git(monkeypatch)
    # No SecurityGateBypassError: the invariant is skipped, not failed.
    asyncio.run(_merge_task_branch(
        str(tmp_path), 3203, "forge-task-3203", expect_artifact=False,
    ))
    assert "event=defensive-invariant-skipped" in capsys.readouterr().err


def test_hermetic_opt_in_is_the_only_way_to_trust_an_unrecorded_artifact(
    tmp_path, require_reviewer_record,
):
    path = _write(_artifact(tmp_path, 3204), _review_body(None, low=1))
    assert verify_reviewer_provenance(3204, path).trusted is False

    set_unrecorded_reviewer_runs_permitted(True)
    verdict = verify_reviewer_provenance(3204, path)
    assert verdict.trusted is True
    assert verdict.reason == "no-reviewer-run-recorded"


def test_production_default_forbids_unrecorded_trust():
    """conftest opts the hermetic suite in; a fresh process must not be."""
    probe = (
        "from equipa.security_gate import unrecorded_reviewer_runs_permitted;"
        "print(unrecorded_reviewer_runs_permitted())"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=True,
    )
    assert result.stdout.strip() == "False"


# ---------------------------------------------------------------------------
# SR41-01: special files at the artifact path are never read
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_fifo_at_artifact_path_does_not_hang(tmp_path):
    """Run in a child process: on 3923301 the read blocks forever."""
    fifo = _artifact(tmp_path, 3205)
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)
    probe = (
        "import sys; from pathlib import Path;"
        "from equipa.security_gate import fingerprint_artifact;"
        "from equipa.loops import _count_findings_in_review_file;"
        "path = Path(sys.argv[1]);"
        "print(fingerprint_artifact(path).exists,"
        " _count_findings_in_review_file(path))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe, str(fifo)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("reading a FIFO at the artifact path blocked")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False None"


def test_symlinked_artifact_is_treated_as_missing(tmp_path):
    real = _write(tmp_path / "elsewhere.md", _review_body(None, low=1))
    link = _artifact(tmp_path, 3206)
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    assert fingerprint_artifact(link).exists is False
    assert _count_findings_in_review_file(link) is None


def test_oversized_artifact_is_treated_as_missing(tmp_path):
    path = _artifact(tmp_path, 3207)
    body = _review_body(None, low=1)
    _write(path, body + "x" * (MAX_REVIEW_ARTIFACT_BYTES + 1 - len(body)))

    assert fingerprint_artifact(path).exists is False
    assert _count_findings_in_review_file(path) is None


def test_regular_artifact_fingerprint_is_exact(tmp_path):
    path = _write(_artifact(tmp_path, 3208), _review_body(None, low=1))
    data = path.read_bytes()

    fingerprint = fingerprint_artifact(path)

    assert fingerprint.exists is True
    assert fingerprint.sha256 == hashlib.sha256(data).hexdigest()
    assert fingerprint.size == len(data)
    assert fingerprint.mtime == path.stat().st_mtime


def test_fallback_dump_is_not_written_through_a_dangling_symlink(tmp_path):
    target = tmp_path / "outside" / "planted.md"
    target.parent.mkdir()
    link = _artifact(tmp_path, 3209)
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    _write_security_review_fallback(link, 3209, "raw agent output", output=[])

    assert not target.exists()


# ---------------------------------------------------------------------------
# SR41-02: the counted bytes are the verified bytes
# ---------------------------------------------------------------------------


def _swap_after_verification(monkeypatch, path: Path, text: str) -> None:
    """A detached writer replaces the file right after provenance passes."""
    real_verify = dispatch.verify_reviewer_provenance

    def verify_then_swap(task_id, review_path):
        verdict = real_verify(task_id, review_path)
        path.write_text(text, encoding="utf-8")
        return verdict

    monkeypatch.setattr(dispatch, "verify_reviewer_provenance", verify_then_swap)


def test_gate_counts_the_bytes_provenance_verified(
    tmp_path, reviewer_harness, monkeypatch, capsys,
):
    _run_reviewer(reviewer_harness, tmp_path, 3210, high=1)
    path = _artifact(tmp_path, 3210)
    verified_sha = _sha16(path.read_bytes())
    _swap_after_verification(monkeypatch, path, _review_body(None, low=0))
    capsys.readouterr()

    blocks, counts = _security_review_blocks_merge(str(tmp_path), 3210)

    assert blocks is True
    assert counts["HIGH"] == 1
    eval_line = next(
        line for line in capsys.readouterr().err.splitlines()
        if "event=blocks-merge-eval" in line
    )
    assert f"sha256={verified_sha}" in eval_line
    assert "H=1" in eval_line


def test_defensive_invariant_counts_the_bytes_provenance_verified(
    tmp_path, reviewer_harness, monkeypatch,
):
    _run_reviewer(reviewer_harness, tmp_path, 3211, high=1)
    path = _artifact(tmp_path, 3211)
    _swap_after_verification(monkeypatch, path, _review_body(None, low=0))
    _stop_before_git(monkeypatch)

    with pytest.raises(SecurityGateBypassError, match="1 HIGH"):
        asyncio.run(_merge_task_branch(
            str(tmp_path), 3211, "forge-task-3211", expect_artifact=True,
        ))


# ---------------------------------------------------------------------------
# SR41-04: the audit names which reviewer ran
# ---------------------------------------------------------------------------


def test_reviewer_run_audit_names_run_id_model_and_prompt_hash(
    tmp_path, reviewer_harness, capsys,
):
    _run_reviewer(reviewer_harness, tmp_path, 3212)

    record = get_reviewer_run(3212)
    prompt_sha = hashlib.sha256(b"prompt").hexdigest()
    assert len(record.run_id) == 16
    assert record.model == "opus"
    assert record.prompt_sha256 == prompt_sha
    run_line = next(
        line for line in capsys.readouterr().err.splitlines()
        if "event=reviewer-run " in line
    )
    assert (
        f"reviewer_run={record.run_id} model=opus "
        f"prompt_sha256={prompt_sha[:16]}"
    ) in run_line


def test_gate_lines_name_the_reviewer_run_they_trusted(
    tmp_path, reviewer_harness, monkeypatch, capsys,
):
    _run_reviewer(reviewer_harness, tmp_path, 3213)
    record = get_reviewer_run(3213)
    identity = f"reviewer_run={record.run_id} model=opus"
    capsys.readouterr()

    _security_review_blocks_merge(str(tmp_path), 3213)
    _stop_before_git(monkeypatch)
    asyncio.run(_merge_task_branch(
        str(tmp_path), 3213, "forge-task-3213", expect_artifact=True,
    ))

    err = capsys.readouterr().err.splitlines()
    for event in ("event=blocks-merge-eval", "event=defensive-invariant-passed"):
        line = next(line for line in err if event in line)
        assert identity in line, line
