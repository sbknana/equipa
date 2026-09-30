"""Task #3041 — edge cases of reviewer-run provenance and reviewer retry.

``test_reviewer_provenance_3041.py`` pins the headline contract (a timed-out
reviewer blocks, only this cycle's artifact is trusted, retry + timeout
scaling). This module covers the helper branches it does not reach:

  * nonce-line matching — the nonce must be on its own line, lowercase,
    exactly 32 hex chars; a nonce quoted inside prose is not proof;
  * every untrusted provenance reason and the GATE-AUDIT event it maps to;
  * the failure-reason and audit-line helpers (reason=unknown, timeouts=-);
  * config coercion (non-integer / non-positive values fall back);
  * diff-size measurement for timeout scaling, including its failure path;
  * run_security_review: overloaded results are not retried, diff size and
    complexity scale the first attempt, each attempt gets a fresh nonce,
    and the persisted stable-path copy logs its provenance.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import equipa.git_ops as git_ops
from equipa import loops
from equipa.agent_runner import OVERLOADED_OUTCOME
from equipa.dispatch import _security_review_blocks_merge
from equipa.loops import (
    ARTIFACTS_DIR_NAME,
    SECURITY_REVIEW_DEFAULT_MAX_ATTEMPTS,
    SECURITY_REVIEW_DEFAULT_TIMEOUT,
    _config_int,
    _measure_review_diff_lines,
    describe_reviewer_failure,
    run_security_review,
)
from equipa.security_gate import (
    REVIEWER_STATUS_FAILED,
    REVIEWER_STATUS_RUNNING,
    REVIEWER_STATUS_SUCCEEDED,
    ArtifactFingerprint,
    ProvenanceVerdict,
    ReviewerRunRecord,
    artifact_nonces,
    audit_reviewer_run,
    fingerprint_artifact,
    get_reviewer_run,
    new_reviewer_nonce,
    record_reviewer_run,
    record_reviewer_skipped_doc_only,
    review_complete_line,
    reviewer_nonce_line,
    reviewer_run_failure,
    verify_reviewer_provenance,
)

NONCE = "0123456789abcdef0123456789abcdef"
NONCE_RE = re.compile(r"EQUIPA-REVIEWER-RUN: ([0-9a-f]{32})")


def _review_body(nonce_line: str | None) -> str:
    """A complete, parser-trusted one-LOW review, optional leading line.

    When the leading line carries a nonce, the completion sentinel for that
    nonce is the last line, as a finished review requires (gate-07).
    """
    header = f"{nonce_line}\n" if nonce_line else ""
    nonce_match = NONCE_RE.search(nonce_line or "")
    trailer = (
        f"{review_complete_line(nonce_match.group(1))}\n" if nonce_match else ""
    )
    return (
        f"{header}# Security Review\n\n"
        f"## Summary\n1 low-severity finding.\n\n"
        f"### [E1] LOW — verbose error message\nDetails.\n\n"
        f"## Counts\n"
        f"CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0\n"
        f"{trailer}"
    )


def _artifact(project_dir: Path, task_id: int) -> Path:
    return project_dir / ARTIFACTS_DIR_NAME / f"SECURITY-REVIEW-{task_id}.md"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _succeeded_record(task_id: int, path: Path, nonce: str = NONCE,
                      pre: ArtifactFingerprint | None = None) -> ReviewerRunRecord:
    """Record a successful run whose post-fingerprint is the file as it is now."""
    record = ReviewerRunRecord(
        task_id=task_id,
        nonce=nonce,
        status=REVIEWER_STATUS_SUCCEEDED,
        started_at=1.0,
        pre_artifact=pre,
        post_artifact=fingerprint_artifact(path),
        attempts=1,
    )
    record_reviewer_run(record)
    return record


# ---------------------------------------------------------------------------
# Nonce line matching
# ---------------------------------------------------------------------------


def test_new_reviewer_nonce_is_32_lowercase_hex_and_unique():
    nonces = {new_reviewer_nonce() for _ in range(50)}
    assert len(nonces) == 50
    assert all(re.fullmatch(r"[0-9a-f]{32}", nonce) for nonce in nonces)


def test_nonce_line_round_trips_through_artifact_nonces():
    assert artifact_nonces(reviewer_nonce_line(NONCE)) == {NONCE}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"  <!-- EQUIPA-REVIEWER-RUN: {NONCE} -->  \nbody", {NONCE}),
        (f"<!--EQUIPA-REVIEWER-RUN:{NONCE}-->", {NONCE}),
        (f"# Title\n\n<!-- EQUIPA-REVIEWER-RUN: {NONCE} -->\n", {NONCE}),
        # Quoted inside prose: not a nonce line, not proof of authorship.
        (f"The reviewer line is <!-- EQUIPA-REVIEWER-RUN: {NONCE} --> here",
         set()),
        # Prose BEFORE the marker: pins the start-of-line anchor.
        (f"Quoted: <!-- EQUIPA-REVIEWER-RUN: {NONCE} -->", set()),
        (f"<!-- EQUIPA-REVIEWER-RUN: {NONCE.upper()} -->", set()),
        (f"<!-- EQUIPA-REVIEWER-RUN: {NONCE[:-1]} -->", set()),
        (f"<!-- EQUIPA-REVIEWER-RUN: {NONCE}0 -->", set()),
        (f"<!-- EQUIPA-REVIEWER-RUN {NONCE} -->", set()),
        ("", set()),
    ],
)
def test_artifact_nonces_only_matches_whole_nonce_lines(text, expected):
    assert artifact_nonces(text) == expected


def test_artifact_nonces_tolerates_none_and_collects_several():
    other = "f" * 32
    text = f"{reviewer_nonce_line(NONCE)}\n{reviewer_nonce_line(other)}\n"
    assert artifact_nonces(None) == set()
    assert artifact_nonces(text) == {NONCE, other}


def test_nonce_quoted_in_prose_is_rejected_by_provenance(tmp_path):
    path = _write(
        _artifact(tmp_path, 4001),
        _review_body(f"Nonce: {reviewer_nonce_line(NONCE)} (quoted)"),
    )
    _succeeded_record(4001, path)

    verdict = verify_reviewer_provenance(4001, path)

    assert (verdict.trusted, verdict.reason) == (False, "reviewer-nonce-missing")


def test_nonce_line_anywhere_on_its_own_line_is_trusted(tmp_path):
    """The prompt asks for the FIRST line, but the gate's proof is the nonce
    itself; a reviewer that put a title above it still wrote the file."""
    body = (
        _review_body(None)
        + f"\n{reviewer_nonce_line(NONCE)}\n{review_complete_line(NONCE)}\n"
    )
    path = _write(_artifact(tmp_path, 4002), body)
    _succeeded_record(4002, path)

    assert verify_reviewer_provenance(4002, path).reason == "verified"


def test_other_tasks_nonce_is_rejected(tmp_path):
    path = _write(_artifact(tmp_path, 4003), _review_body(reviewer_nonce_line(NONCE)))
    _succeeded_record(4003, path, nonce="a" * 32)

    verdict = verify_reviewer_provenance(4003, path)

    assert (verdict.trusted, verdict.reason) == (False, "reviewer-nonce-missing")


# ---------------------------------------------------------------------------
# Provenance reasons and their GATE-AUDIT events
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason", "event"),
    [
        (REVIEWER_STATUS_FAILED, "reviewer-failed", "reviewer-failed"),
        (REVIEWER_STATUS_RUNNING, "reviewer-running", "reviewer-failed"),
    ],
)
def test_unfinished_or_failed_run_rejects_even_a_nonce_bearing_file(
    tmp_path, status, reason, event,
):
    """A run that did not succeed is untrusted even if its artifact is
    perfect — the partial file of a timed-out run carries its nonce."""
    path = _write(_artifact(tmp_path, 4010), _review_body(reviewer_nonce_line(NONCE)))
    record_reviewer_run(ReviewerRunRecord(
        task_id=4010, nonce=NONCE, status=status, started_at=1.0,
        post_artifact=fingerprint_artifact(path),
    ))

    verdict = verify_reviewer_provenance(4010, path)

    assert (verdict.trusted, verdict.reason) == (False, reason)
    assert verdict.audit_event == event


def test_doc_only_skip_is_an_artifact_rejection_not_a_reviewer_failure(tmp_path):
    path = _write(_artifact(tmp_path, 4011), _review_body(reviewer_nonce_line(NONCE)))
    record_reviewer_skipped_doc_only(4011)

    verdict = verify_reviewer_provenance(4011, path)

    assert (verdict.trusted, verdict.reason) == (False, "reviewer-skipped-doc-only")
    assert verdict.audit_event == "artifact-provenance-rejected"
    # A doc-only skip is not a reviewer failure: the call sites must not
    # report it as one.
    assert reviewer_run_failure(4011) is None


def test_reviewer_left_nothing_at_its_path_is_rejected(tmp_path):
    """The reviewer finished with no file, then one appeared afterwards."""
    path = _artifact(tmp_path, 4012)
    _succeeded_record(4012, path)  # post fingerprint: exists=False
    _write(path, _review_body(reviewer_nonce_line(NONCE)))

    verdict = verify_reviewer_provenance(4012, path)

    assert (verdict.trusted, verdict.reason) == (
        False, "artifact-not-written-by-reviewer",
    )
    assert verdict.audit_event == "artifact-provenance-rejected"


def test_record_without_post_fingerprint_is_rejected(tmp_path):
    path = _write(_artifact(tmp_path, 4013), _review_body(reviewer_nonce_line(NONCE)))
    record_reviewer_run(ReviewerRunRecord(
        task_id=4013, nonce=NONCE, status=REVIEWER_STATUS_SUCCEEDED,
        started_at=1.0, post_artifact=None,
    ))

    assert verify_reviewer_provenance(4013, path).reason == (
        "artifact-not-written-by-reviewer"
    )


def test_artifact_deleted_after_review_is_missing(tmp_path):
    path = _write(_artifact(tmp_path, 4014), _review_body(reviewer_nonce_line(NONCE)))
    _succeeded_record(4014, path)
    path.unlink()

    verdict = verify_reviewer_provenance(4014, path)

    assert (verdict.trusted, verdict.reason) == (False, "artifact-missing")
    assert verdict.fingerprint.exists is False


def test_reviewer_overwriting_the_developer_file_is_trusted(tmp_path):
    """Pre-existing self-review replaced by the reviewer's own → verified."""
    path = _write(_artifact(tmp_path, 4015), _review_body(None))
    pre = fingerprint_artifact(path)
    _write(path, _review_body(reviewer_nonce_line(NONCE)))
    _succeeded_record(4015, path, pre=pre)

    assert verify_reviewer_provenance(4015, path).reason == "verified"


def test_pre_existing_check_runs_before_nonce_check(tmp_path):
    """Unchanged pre-existing bytes report artifact-pre-existing even when
    the file happens to carry the run's nonce (reason names the real cause)."""
    path = _write(_artifact(tmp_path, 4016), _review_body(reviewer_nonce_line(NONCE)))
    pre = fingerprint_artifact(path)
    _succeeded_record(4016, path, pre=pre)

    assert verify_reviewer_provenance(4016, path).reason == "artifact-pre-existing"


def test_task_id_int_and_str_share_one_record(tmp_path):
    record_reviewer_run(ReviewerRunRecord(
        task_id=4017, nonce=NONCE, status=REVIEWER_STATUS_FAILED,
        started_at=1.0, failure_reason="timeout",
    ))
    assert get_reviewer_run("4017") is get_reviewer_run(4017)
    assert reviewer_run_failure("4017") == "timeout"


def test_new_record_replaces_previous_run_for_same_task():
    record_reviewer_run(ReviewerRunRecord(
        task_id=4018, nonce=NONCE, status=REVIEWER_STATUS_FAILED,
        started_at=1.0, failure_reason="timeout",
    ))
    record_reviewer_run(ReviewerRunRecord(
        task_id=4018, nonce=NONCE, status=REVIEWER_STATUS_SUCCEEDED,
        started_at=2.0,
    ))
    assert reviewer_run_failure(4018) is None


@pytest.mark.parametrize(
    ("status", "failure_reason", "expected"),
    [
        (REVIEWER_STATUS_FAILED, "timeout", "timeout"),
        (REVIEWER_STATUS_FAILED, None, REVIEWER_STATUS_FAILED),
        (REVIEWER_STATUS_RUNNING, None, "crashed"),
        (REVIEWER_STATUS_RUNNING, "timeout", "crashed"),
        (REVIEWER_STATUS_SUCCEEDED, None, None),
    ],
)
def test_reviewer_run_failure_reasons(status, failure_reason, expected):
    record_reviewer_run(ReviewerRunRecord(
        task_id=4019, nonce=NONCE, status=status, started_at=1.0,
        failure_reason=failure_reason,
    ))
    assert reviewer_run_failure(4019) == expected


def test_reviewer_run_failure_is_none_without_a_record():
    assert reviewer_run_failure(4020) is None


def test_provenance_verdict_audit_event_mapping():
    fingerprint = ArtifactFingerprint(path="x", exists=False)
    assert ProvenanceVerdict(False, "reviewer-failed", fingerprint).audit_event == (
        "reviewer-failed"
    )
    for reason in ("artifact-missing", "artifact-pre-existing",
                   "reviewer-nonce-missing", "artifact-changed-after-review"):
        assert ProvenanceVerdict(False, reason, fingerprint).audit_event == (
            "artifact-provenance-rejected"
        )


# ---------------------------------------------------------------------------
# Fingerprints and audit lines
# ---------------------------------------------------------------------------


def test_fingerprint_describes_path_hash_prefix_mtime_and_size(tmp_path):
    path = _write(tmp_path / "review.md", "hello")
    fingerprint = fingerprint_artifact(path)
    digest = hashlib.sha256(b"hello").hexdigest()

    assert fingerprint.exists and fingerprint.size == 5
    assert fingerprint.sha256 == digest
    assert fingerprint.describe() == (
        f"artifact={path} sha256={digest[:16]} "
        f"mtime={path.stat().st_mtime:.3f} size=5"
    )


def test_fingerprint_of_directory_is_absent(tmp_path):
    """A directory at the artifact path is not a review: fail closed."""
    directory = tmp_path / "SECURITY-REVIEW-1.md"
    directory.mkdir()
    assert fingerprint_artifact(directory).exists is False


def test_audit_line_for_failed_run_without_reason_or_artifact(capsys):
    audit_reviewer_run(ReviewerRunRecord(
        task_id=4030, nonce=NONCE, status=REVIEWER_STATUS_FAILED,
        started_at=1.0, attempts=0, duration=0.0,
    ))
    err = capsys.readouterr().err
    assert "task=4030 event=reviewer-failed status=failed attempts=0" in err
    assert "duration=0.0s timeouts=- nonce=01234567" in err
    assert "artifact=unknown" in err
    assert "reason=unknown" in err


def test_audit_line_for_successful_run_has_no_reason(tmp_path, capsys):
    path = _write(tmp_path / "SECURITY-REVIEW-4031.md", "x")
    audit_reviewer_run(ReviewerRunRecord(
        task_id=4031, nonce=NONCE, status=REVIEWER_STATUS_SUCCEEDED,
        started_at=1.0, post_artifact=fingerprint_artifact(path),
        attempts=2, duration=1234.56, timeouts=(900, 1350),
    ))
    err = capsys.readouterr().err
    assert "event=reviewer-run status=succeeded attempts=2" in err
    assert "duration=1234.6s timeouts=900,1350" in err
    assert f"artifact={path}" in err
    assert "reason=" not in err


# ---------------------------------------------------------------------------
# Failure description and config coercion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (None, "no-result"),
        ({}, "no-result"),
        ({"success": False, "outcome": OVERLOADED_OUTCOME,
          "errors": ["Process timed out after 900 seconds"]}, "overloaded"),
        ({"success": False,
          "errors": ["Process Timed Out after 900 seconds"]}, "timeout"),
        ({"success": False, "errors": ["exit code 1"]}, "agent-error"),
        ({"success": False, "errors": []}, "unsuccessful"),
        ({"success": False, "errors": None}, "unsuccessful"),
    ],
)
def test_describe_reviewer_failure(result, expected):
    assert describe_reviewer_failure(result) == expected


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (None, 900),
        ({}, 900),
        ({"k": 1200}, 1200),
        ({"k": "1800"}, 1800),
        ({"k": "abc"}, 900),
        ({"k": None}, 900),
        ({"k": [1]}, 900),
        ({"k": 0}, 900),
        ({"k": -5}, 900),
    ],
)
def test_config_int_falls_back_on_bad_values(config, expected):
    assert _config_int(config, "k", 900) == expected


# ---------------------------------------------------------------------------
# Diff-size measurement
# ---------------------------------------------------------------------------


def _patch_git(monkeypatch, *, stdout="", returncode=0, calls=None):
    async def fake_git(args, cwd, timeout=None):
        if calls is not None:
            calls.append((args, cwd, timeout))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(git_ops, "get_trusted_default_branch", lambda _d: "main")
    monkeypatch.setattr(loops, "git_run_async", fake_git)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (" 3 files changed, 120 insertions(+), 30 deletions(-)\n", 150),
        (" 1 file changed, 1 insertion(+)\n", 1),
        (" 2 files changed, 7 deletions(-)\n", 7),
        ("", 0),
    ],
)
def test_measure_review_diff_lines_sums_insertions_and_deletions(
    monkeypatch, stdout, expected,
):
    calls: list = []
    _patch_git(monkeypatch, stdout=stdout, calls=calls)

    assert asyncio.run(_measure_review_diff_lines("/repo")) == expected
    assert calls == [(["diff", "--shortstat", "main...HEAD"], "/repo", 10)]


def test_measure_review_diff_lines_is_zero_on_git_failure(monkeypatch):
    _patch_git(monkeypatch, stdout=" 1 file changed, 9999 insertions(+)",
               returncode=128)
    assert asyncio.run(_measure_review_diff_lines("/repo")) == 0


def test_measure_review_diff_lines_is_zero_without_trusted_branch(monkeypatch):
    def no_branch(_project_dir):
        raise RuntimeError("no trusted default branch")

    monkeypatch.setattr(git_ops, "get_trusted_default_branch", no_branch)

    assert asyncio.run(_measure_review_diff_lines("/repo")) == 0


# ---------------------------------------------------------------------------
# run_security_review — retry policy, scaling, persisted provenance
# ---------------------------------------------------------------------------


@pytest.fixture
def reviewer(monkeypatch):
    """Script run_security_review's agent layer, one behaviour per attempt."""
    harness = SimpleNamespace(
        behaviours=[], timeouts=[], nonces=[], task_id=None, diff_lines=0,
        config={"security_review_timeout": 30},
    )

    async def fake_run_agent(_cmd, timeout=None):
        harness.timeouts.append(timeout)
        nonce = get_reviewer_run(harness.task_id).nonce
        harness.nonces.append(nonce)
        return harness.behaviours[len(harness.timeouts) - 1](harness.task_id, nonce)

    @contextlib.contextmanager
    def fake_cli(*_args, **_kwargs):
        yield ["claude"]

    async def fake_diff(_project_dir):
        return harness.diff_lines

    monkeypatch.setattr(loops, "run_agent", fake_run_agent)
    monkeypatch.setattr(loops, "build_cli_command", fake_cli)
    monkeypatch.setattr(loops, "build_system_prompt", lambda *a, **kw: "prompt")
    monkeypatch.setattr(loops, "get_role_turns", lambda *a, **kw: 5)
    monkeypatch.setattr(loops, "get_role_model", lambda *a, **kw: "opus")
    monkeypatch.setattr(loops, "load_dispatch_config", lambda _p: harness.config)
    monkeypatch.setattr(loops, "_measure_review_diff_lines", fake_diff)
    monkeypatch.setattr(loops, "_extract_security_findings", lambda _t: [])
    return harness


def _run(task_id: int, worktree: Path, *, stable: Path | None = None,
         complexity: str | None = None, output: list | None = None) -> None:
    task = {"id": task_id, "title": "t", "description": "d", "project_id": 1}
    if complexity:
        task["complexity"] = complexity
    asyncio.run(run_security_review(
        task, str(worktree), {}, SimpleNamespace(dispatch_config=None),
        output=output if output is not None else [],
        stable_project_dir=str(stable) if stable else None,
    ))


def _writes_review(project_dir: Path):
    def behaviour(task_id, nonce):
        _write(_artifact(project_dir, task_id),
               _review_body(reviewer_nonce_line(nonce)))
        return {"success": True, "result_text": "done", "errors": []}
    return behaviour


def _overloaded(_task_id, _nonce):
    return {"success": False, "outcome": OVERLOADED_OUTCOME,
            "errors": ["sustained 529"], "result_text": ""}


def _times_out(_task_id, _nonce):
    return {"success": False, "errors": ["Process timed out after 30 seconds"],
            "result_text": ""}


def test_overloaded_reviewer_is_not_retried(tmp_path, reviewer):
    reviewer.task_id = 4040
    reviewer.behaviours = [_overloaded, _writes_review(tmp_path)]

    _run(4040, tmp_path)

    assert reviewer.timeouts == [30]
    assert reviewer_run_failure(4040) == "overloaded"
    assert _security_review_blocks_merge(
        str(tmp_path), 4040, block_on_missing=False,
    ) == (True, None)


def test_every_attempt_times_out_blocks_after_two_attempts(
    tmp_path, reviewer, capsys,
):
    reviewer.task_id = 4041
    reviewer.behaviours = [_times_out, _times_out, _writes_review(tmp_path)]
    output: list[str] = []

    _run(4041, tmp_path, output=output)

    assert reviewer.timeouts == [30, 45]
    assert len(set(reviewer.nonces)) == 2
    record = get_reviewer_run(4041)
    assert record.status == REVIEWER_STATUS_FAILED
    assert (record.attempts, record.timeouts) == (2, (30, 45))
    assert record.failure_reason == "timeout"
    assert record.nonce == reviewer.nonces[-1]
    err = capsys.readouterr().err
    assert "event=reviewer-failed status=failed attempts=2" in err
    assert "timeouts=30,45" in err and "reason=timeout" in err
    joined = "\n".join(str(line) for line in output)
    assert "failed after 2 attempt(s)" in joined
    assert "regardless of any artifact on disk" in joined


def test_diff_size_scales_the_first_attempt_timeout(tmp_path, reviewer):
    reviewer.task_id = 4042
    reviewer.diff_lines = 1500
    reviewer.behaviours = [_writes_review(tmp_path)]

    _run(4042, tmp_path)

    assert reviewer.timeouts == [60]


def test_complex_task_gets_triple_timeout_and_is_capped(tmp_path, reviewer):
    reviewer.config = {"security_review_timeout": 900,
                       "security_review_timeout_max": 2000}
    reviewer.task_id = 4043
    reviewer.behaviours = [_times_out, _writes_review(tmp_path)]

    _run(4043, tmp_path, complexity="complex")

    # 900 * 3 = 2700 capped at 2000; the retry cannot exceed the cap either.
    assert reviewer.timeouts == [2000, 2000]


def test_invalid_config_uses_documented_defaults(tmp_path, reviewer):
    reviewer.config = {"security_review_timeout": "fifteen minutes",
                       "security_review_max_attempts": 0}
    reviewer.task_id = 4044
    reviewer.behaviours = [_times_out] * SECURITY_REVIEW_DEFAULT_MAX_ATTEMPTS

    _run(4044, tmp_path)

    assert reviewer.timeouts[0] == SECURITY_REVIEW_DEFAULT_TIMEOUT
    assert len(reviewer.timeouts) == SECURITY_REVIEW_DEFAULT_MAX_ATTEMPTS


def test_successful_run_is_audited_as_reviewer_run(tmp_path, reviewer, capsys):
    reviewer.task_id = 4045
    reviewer.behaviours = [_writes_review(tmp_path)]

    _run(4045, tmp_path)

    err = capsys.readouterr().err
    assert "task=4045 event=reviewer-run status=succeeded attempts=1" in err
    assert "timeouts=30" in err
    blocks, counts = _security_review_blocks_merge(str(tmp_path), 4045)
    assert blocks is False
    assert counts is not None


def test_persisted_copy_logs_provenance_of_the_stable_file(tmp_path, reviewer):
    worktree = tmp_path / "worktree"
    stable = tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()
    reviewer.task_id = 4046
    reviewer.behaviours = [_writes_review(worktree)]
    output: list[str] = []

    _run(4046, worktree, stable=stable, output=output)

    persisted = [str(line) for line in output if "Persisted security-review" in str(line)]
    assert persisted, output
    assert "provenance=verified" in persisted[-1]
    assert f"artifact={_artifact(stable, 4046)}" in persisted[-1]
    # The stable copy is byte-identical, so the gate trusts it too.
    assert _security_review_blocks_merge(str(stable), 4046)[0] is False


def test_persisted_developer_review_after_timeout_logs_rejection(
    tmp_path, reviewer,
):
    """The exact #3035 log line: after a timeout the copied file must no
    longer read as a plain 'structured artifact'."""
    worktree = tmp_path / "worktree"
    stable = tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()
    _write(_artifact(worktree, 4047), _review_body(None))
    reviewer.task_id = 4047
    reviewer.behaviours = [_times_out, _times_out]
    output: list[str] = []

    _run(4047, worktree, stable=stable, output=output)

    # run_security_review persists the worktree file on EVERY outcome.
    persisted = [str(line) for line in output if "Persisted security-review" in str(line)]
    assert persisted, output
    assert "structured artifact" in persisted[-1]
    assert "provenance=reviewer-failed" in persisted[-1]
    assert _security_review_blocks_merge(
        str(stable), 4047, block_on_missing=False,
    ) == (True, None)
