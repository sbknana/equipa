"""Task #3041 — the merge gate must only trust THIS cycle's reviewer artifact.

Observed on CryptoTrader #3035: the security reviewer timed out after 900s,
the orchestrator copied the DEVELOPER's self-review (committed on the task
branch at the reviewer's artifact path) to the stable path, and the gate
parsed it as the reviewer's verdict and merged with reason=clean.

These tests pin the fail-closed contract:
  1. a failed / timed-out reviewer blocks regardless of any artifact on disk
     (and of block_on_missing), with GATE-AUDIT event=reviewer-failed;
  2. only an artifact written by the reviewer run of this cycle is accepted
     (nonce line + pre/post fingerprints);
  3. the gate logs which file and hash its counts came from;
  4. the reviewer is retried once with a longer timeout, and the timeout
     scales with complexity and diff size.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from equipa import cli, loops
from equipa.dispatch import (
    _gated_merge_task,
    _merge_task_branch,
    _security_review_blocks_merge,
)
import equipa.dispatch as dispatch
from equipa.loops import (
    ARTIFACTS_DIR_NAME,
    compute_security_review_retry_timeout,
    compute_security_review_timeout,
    run_security_review,
)
from equipa.security_gate import (
    REVIEWER_STATUS_FAILED,
    REVIEWER_STATUS_SUCCEEDED,
    ReviewerRunRecord,
    SecurityGateBypassError,
    fingerprint_artifact,
    get_reviewer_run,
    record_reviewer_run,
    record_reviewer_skipped_doc_only,
    reviewer_nonce_line,
    reviewer_run_failure,
    verify_reviewer_provenance,
)

TIMEOUT_RESULT = {
    "success": False,
    "errors": ["Process timed out after 30 seconds"],
    "result_text": "",
}


def _review_body(nonce: str | None, *, low: int = 1) -> str:
    """A complete, parser-trusted review; nonce line first when given."""
    header = f"{reviewer_nonce_line(nonce)}\n" if nonce else ""
    findings = "".join(
        f"### [R{index}] LOW — verbose error message {index}\n"
        f"Details of finding {index}.\n\n"
        for index in range(1, low + 1)
    )
    return (
        f"{header}# Security Review\n\n"
        f"## Summary\n{low} low-severity finding(s).\n\n"
        f"{findings}"
        f"## Counts\n"
        f"CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: {low} | INFO: 0\n"
    )


def _artifact(project_dir: Path, task_id: int) -> Path:
    return project_dir / ARTIFACTS_DIR_NAME / f"SECURITY-REVIEW-{task_id}.md"


def _write_developer_self_review(project_dir: Path, task_id: int) -> Path:
    """What the #3035 developer committed: clean, well-formed, no nonce."""
    path = _artifact(project_dir, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_review_body(None, low=2), encoding="utf-8")
    return path


@pytest.fixture
def reviewer_harness(monkeypatch):
    """Patch the agent layer of run_security_review; script each attempt.

    ``harness.behaviours`` is a list of callables ``(task_id, nonce) ->
    result``, one per attempt. ``harness.timeouts`` records the timeout
    passed to each run_agent call, ``harness.prompts`` each description.
    """
    harness = SimpleNamespace(
        behaviours=[], timeouts=[], prompts=[], task_id=None,
        config={"security_review_timeout": 30},
    )

    async def fake_run_agent(_cmd, timeout=None):
        harness.timeouts.append(timeout)
        attempt = len(harness.timeouts) - 1
        record = get_reviewer_run(harness.task_id)
        return harness.behaviours[attempt](harness.task_id, record.nonce)

    @contextlib.contextmanager
    def fake_cli(*_args, **_kwargs):
        yield ["claude"]

    def fake_prompt(security_task, *_args, **_kwargs):
        harness.prompts.append(security_task["description"])
        return "prompt"

    async def no_diff(_project_dir):
        return 0

    monkeypatch.setattr(loops, "run_agent", fake_run_agent)
    monkeypatch.setattr(loops, "build_cli_command", fake_cli)
    monkeypatch.setattr(loops, "build_system_prompt", fake_prompt)
    monkeypatch.setattr(loops, "get_role_turns", lambda *a, **kw: 5)
    monkeypatch.setattr(loops, "get_role_model", lambda *a, **kw: "opus")
    monkeypatch.setattr(loops, "load_dispatch_config", lambda _p: harness.config)
    monkeypatch.setattr(loops, "_measure_review_diff_lines", no_diff)
    monkeypatch.setattr(loops, "_extract_security_findings", lambda _t: [])
    return harness


def _run_review(task_id: int, worktree: Path, stable: Path | None = None,
                output: list[str] | None = None) -> dict:
    task = {"id": task_id, "title": "t", "description": "d", "project_id": 1}
    return asyncio.run(run_security_review(
        task, str(worktree), {}, SimpleNamespace(dispatch_config=None),
        output=output if output is not None else [],
        stable_project_dir=str(stable) if stable else None,
    ))


def _writes_review(project_dir: Path, *, with_nonce: bool = True):
    def behaviour(task_id, nonce):
        path = _artifact(project_dir, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _review_body(nonce if with_nonce else None), encoding="utf-8",
        )
        return {"success": True, "result_text": "done", "errors": []}
    return behaviour


def _times_out(_task_id, _nonce):
    return dict(TIMEOUT_RESULT)


def _succeeds_without_writing(_task_id, _nonce):
    return {"success": True, "result_text": "done", "errors": []}


# ---------------------------------------------------------------------------
# (1) failed / timed-out reviewer blocks regardless of the artifact
# ---------------------------------------------------------------------------


def test_3035_timeout_with_developer_self_review_blocks(
    tmp_path, reviewer_harness, capsys,
):
    """Exact #3035 shape: dev self-review on disk, reviewer times out twice,
    artifact copied to the stable path — the gate must BLOCK."""
    worktree, stable = tmp_path / "wt", tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()
    _write_developer_self_review(worktree, 3035)
    reviewer_harness.task_id = 3035
    reviewer_harness.behaviours = [_times_out, _times_out]

    _run_review(3035, worktree, stable)

    assert reviewer_run_failure(3035) == "timeout"
    # block_on_missing=False must NOT relax this: the file is not a review.
    blocks, counts = _security_review_blocks_merge(
        str(stable), 3035, block_on_missing=False,
    )
    assert (blocks, counts) == (True, None)
    err = capsys.readouterr().err
    assert "event=reviewer-failed" in err
    assert "provenance=reviewer-failed" in err


def test_gated_merge_blocks_on_failed_reviewer_even_if_outcome_not_demoted(
    tmp_path, monkeypatch,
):
    """Ground-truth gate: a caller that forgot to demote the outcome still
    cannot merge past a failed reviewer run."""
    _write_developer_self_review(tmp_path, 3100)
    record_reviewer_run(ReviewerRunRecord(
        task_id=3100, nonce="0" * 32, status=REVIEWER_STATUS_FAILED,
        started_at=0.0, failure_reason="timeout",
    ))

    async def code_diff(*_args, **_kwargs):
        return ["src/app.py"]

    async def must_not_merge(*_args, **_kwargs):
        raise AssertionError("merge attempted past a failed reviewer")

    monkeypatch.setattr(dispatch, "get_changed_files_for_branch", code_diff)
    monkeypatch.setattr(dispatch, "_merge_task_branch", must_not_merge)

    status = asyncio.run(_gated_merge_task(
        repo=tmp_path, branch="forge-task-3100", outcome="tests_passed",
        task_id=3100, block_on_missing=False,
    ))
    assert status == "blocked"


def test_defensive_invariant_rejects_untrusted_artifact(tmp_path):
    _write_developer_self_review(tmp_path, 3101)
    record_reviewer_run(ReviewerRunRecord(
        task_id=3101, nonce="0" * 32, status=REVIEWER_STATUS_FAILED,
        started_at=0.0, failure_reason="timeout",
    ))
    with pytest.raises(SecurityGateBypassError, match="reviewer-failed"):
        asyncio.run(_merge_task_branch(
            str(tmp_path), 3101, "forge-task-3101", expect_artifact=True,
        ))


def test_single_task_call_site_demotes_outcome_on_failed_reviewer(
    tmp_path, monkeypatch, capsys,
):
    _write_developer_self_review(tmp_path, 3102)
    merge_outcomes: list[str] = []

    async def failing_review(task, *_args, **_kwargs):
        record_reviewer_run(ReviewerRunRecord(
            task_id=task["id"], nonce="1" * 32, status=REVIEWER_STATUS_FAILED,
            started_at=0.0, attempts=2, failure_reason="timeout",
        ))
        return dict(TIMEOUT_RESULT)

    async def code_diff(*_args, **_kwargs):
        return ["src/app.py"]

    async def fake_post_merge(**kwargs):
        merge_outcomes.append(kwargs["outcome"])
        return "skipped"

    monkeypatch.setattr(cli, "run_security_review", failing_review)
    monkeypatch.setattr(cli, "get_changed_files_for_branch", code_diff)
    monkeypatch.setattr(cli, "is_security_review_enabled", lambda _a: True)
    monkeypatch.setattr(cli, "_gated_post_merge", fake_post_merge)
    args = SimpleNamespace(dispatch_config={"security_review": True},
                           dev_test=True)

    outcome = asyncio.run(cli._run_security_review_and_gate(
        {"id": 3102}, str(tmp_path), {}, args, "tests_passed"))

    assert outcome == "security_review_blocked"
    assert merge_outcomes == ["security_review_blocked"]
    # The operator must see WHY: a reviewer failure, not a finding count or
    # a "missing artifact" (the developer's file IS on disk).
    assert "security reviewer FAILED (timeout)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# (2) only an artifact written by this cycle's reviewer run is accepted
# ---------------------------------------------------------------------------


def test_reviewer_written_artifact_with_nonce_is_trusted(
    tmp_path, reviewer_harness,
):
    reviewer_harness.task_id = 3103
    reviewer_harness.behaviours = [_writes_review(tmp_path)]

    _run_review(3103, tmp_path)

    record = get_reviewer_run(3103)
    assert record.status == REVIEWER_STATUS_SUCCEEDED
    assert reviewer_nonce_line(record.nonce) in reviewer_harness.prompts[0]
    blocks, counts = _security_review_blocks_merge(str(tmp_path), 3103)
    assert blocks is False
    assert counts["LOW"] == 1


@pytest.mark.parametrize(
    ("behaviour_name", "expected_reason"),
    [
        ("untouched_pre_existing", "artifact-pre-existing"),
        ("written_without_nonce", "reviewer-nonce-missing"),
        ("nothing_written", "artifact-not-written-by-reviewer"),
    ],
)
def test_reviewer_success_but_untrusted_artifact_blocks(
    tmp_path, reviewer_harness, capsys, behaviour_name, expected_reason,
):
    if behaviour_name == "untouched_pre_existing":
        _write_developer_self_review(tmp_path, 3104)
        behaviour = _succeeds_without_writing
    elif behaviour_name == "written_without_nonce":
        behaviour = _writes_review(tmp_path, with_nonce=False)
    else:
        behaviour = _succeeds_without_writing
    reviewer_harness.task_id = 3104
    reviewer_harness.behaviours = [behaviour]

    _run_review(3104, tmp_path)

    verdict = verify_reviewer_provenance(3104, _artifact(tmp_path, 3104))
    assert (verdict.trusted, verdict.reason) == (False, expected_reason)
    blocks, counts = _security_review_blocks_merge(
        str(tmp_path), 3104, block_on_missing=False,
    )
    assert (blocks, counts) == (True, None)
    assert f"reason={expected_reason}" in capsys.readouterr().err


def test_artifact_edited_after_review_is_rejected(tmp_path, reviewer_harness):
    reviewer_harness.task_id = 3105
    reviewer_harness.behaviours = [_writes_review(tmp_path)]
    _run_review(3105, tmp_path)
    artifact = _artifact(tmp_path, 3105)
    artifact.write_text(
        artifact.read_text(encoding="utf-8").replace("LOW: 1", "LOW: 0"),
        encoding="utf-8",
    )

    verdict = verify_reviewer_provenance(3105, artifact)
    assert verdict.reason == "artifact-changed-after-review"
    assert _security_review_blocks_merge(str(tmp_path), 3105) == (True, None)


def test_doc_only_skip_then_code_diff_gate_blocks(tmp_path):
    """Caller skipped the reviewer as doc-only but the gate sees code: the
    developer's artifact must not stand in for the missing review."""
    _write_developer_self_review(tmp_path, 3106)
    record_reviewer_skipped_doc_only(3106)
    blocks, _ = _security_review_blocks_merge(
        str(tmp_path), 3106, block_on_missing=False,
    )
    assert blocks is True


def test_no_recorded_run_keeps_artifact_only_behaviour(tmp_path, capsys):
    """Direct gate callers with no reviewer run keep pre-#3041 semantics,
    and the audit line says the provenance was not verified."""
    _write_developer_self_review(tmp_path, 3107)
    blocks, counts = _security_review_blocks_merge(str(tmp_path), 3107)
    assert blocks is False and counts["LOW"] == 2
    assert "provenance=no-reviewer-run-recorded" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# (3) the gate logs which file and hash the counts came from
# ---------------------------------------------------------------------------


def test_blocks_merge_eval_logs_artifact_path_and_sha256(
    tmp_path, reviewer_harness, capsys,
):
    reviewer_harness.task_id = 3108
    reviewer_harness.behaviours = [_writes_review(tmp_path)]
    output: list[str] = []
    _run_review(3108, tmp_path, output=output)
    artifact = _artifact(tmp_path, 3108)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()[:16]

    _security_review_blocks_merge(str(tmp_path), 3108)

    eval_lines = [
        line for line in capsys.readouterr().err.splitlines()
        if "event=blocks-merge-eval" in line
    ]
    assert eval_lines, "no blocks-merge-eval audit line"
    assert f"artifact={artifact}" in eval_lines[-1]
    assert f"sha256={digest}" in eval_lines[-1]
    assert "provenance=verified" in eval_lines[-1]
    assert any(f"sha256={digest}" in line and "counts" in line
               for line in output), output


# ---------------------------------------------------------------------------
# (4) retry once with a longer timeout; timeout scales; duration audited
# ---------------------------------------------------------------------------


def test_timeout_then_success_retries_once_with_longer_timeout(
    tmp_path, reviewer_harness, capsys,
):
    reviewer_harness.task_id = 3109
    reviewer_harness.behaviours = [_times_out, _writes_review(tmp_path)]

    _run_review(3109, tmp_path)

    assert reviewer_harness.timeouts == [30, 45]
    record = get_reviewer_run(3109)
    assert record.status == REVIEWER_STATUS_SUCCEEDED
    assert record.attempts == 2 and record.timeouts == (30, 45)
    # Each attempt gets its own nonce; only the final one is accepted.
    assert reviewer_nonce_line(record.nonce) in reviewer_harness.prompts[1]
    assert reviewer_nonce_line(record.nonce) not in reviewer_harness.prompts[0]
    assert _security_review_blocks_merge(str(tmp_path), 3109)[0] is False
    audit = capsys.readouterr().err
    assert "event=reviewer-run status=succeeded attempts=2" in audit
    assert "timeouts=30,45" in audit and "duration=" in audit


def test_partial_file_from_timed_out_attempt_is_not_accepted(
    tmp_path, reviewer_harness,
):
    """Attempt 1 writes a file then times out; attempt 2 succeeds without
    rewriting it. The stale nonce belongs to a failed attempt → reject."""
    first_writer = _writes_review(tmp_path)

    def writes_then_times_out(task_id, nonce):
        first_writer(task_id, nonce)
        return dict(TIMEOUT_RESULT)

    reviewer_harness.task_id = 3110
    reviewer_harness.behaviours = [writes_then_times_out, _succeeds_without_writing]

    _run_review(3110, tmp_path)

    verdict = verify_reviewer_provenance(3110, _artifact(tmp_path, 3110))
    assert (verdict.trusted, verdict.reason) == (False, "reviewer-nonce-missing")


def test_retry_disabled_by_config_runs_once(tmp_path, reviewer_harness):
    reviewer_harness.config = {
        "security_review_timeout": 30, "security_review_max_attempts": 1,
    }
    reviewer_harness.task_id = 3111
    reviewer_harness.behaviours = [_times_out]

    _run_review(3111, tmp_path)

    assert reviewer_harness.timeouts == [30]
    assert reviewer_run_failure(3111) == "timeout"


@pytest.mark.parametrize(
    ("base", "complexity", "changed_lines", "max_timeout", "expected"),
    [
        (900, "simple", 0, 5400, 900),
        (900, "medium", 0, 5400, 1350),
        (900, "complex", 0, 5400, 2700),
        (900, "epic", 0, 5400, 2700),
        (900, "simple", 1500, 5400, 1800),
        (900, "simple", 4000, 5400, 2700),
        (900, "complex", 5000, 5400, 2700),
        (900, "complex", 0, 1200, 1200),     # capped
        (2000, "complex", 0, 1200, 2000),    # explicit base never reduced
        (900, "unknown", 0, 5400, 900),
    ],
)
def test_compute_security_review_timeout(
    base, complexity, changed_lines, max_timeout, expected,
):
    assert compute_security_review_timeout(
        base, complexity, changed_lines, max_timeout,
    ) == expected


@pytest.mark.parametrize(
    ("timeout", "max_timeout", "expected"),
    [(900, 5400, 1350), (2700, 5400, 4050), (5000, 5400, 5400), (5400, 5400, 5400)],
)
def test_compute_security_review_retry_timeout(timeout, max_timeout, expected):
    assert compute_security_review_retry_timeout(timeout, max_timeout) == expected


def test_fingerprint_of_missing_artifact(tmp_path):
    fingerprint = fingerprint_artifact(tmp_path / "absent.md")
    assert fingerprint.exists is False
    assert "exists=False" in fingerprint.describe()
