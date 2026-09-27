"""Task #3063 (GATE-PROVENANCE-2) — tester edge cases.

Complements tests/test_gate_provenance_3063.py with the paths it does not
pin down:

  * SR41-01 backstops: an entry swapped in AFTER the ``lstat`` check (FIFO or
    symlink) is still refused by ``O_NONBLOCK`` / ``O_NOFOLLOW`` / ``fstat``;
    the size cap is exact at its boundary; a FIFO reaching the full gate is
    reported ``artifact_exists=False`` instead of hanging.
  * SR41-01 persistence: a symlinked worktree artifact is not copied (its
    target's bytes never reach the stable path).
  * SR41-02: ``run_security_review``'s own counts come from the verified
    bytes; ``text=`` really replaces the path read.
  * SR41-03: the no-record audit line says ``reviewer_run=none``.
  * SR41-04: one run id and model across retry attempts, the prompt hash of
    the attempt that actually ran last, and identity on ``reviewer-failed``.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

import equipa.loops as loops
from equipa.dispatch import _security_review_blocks_merge
from equipa.loops import (
    ARTIFACTS_DIR_NAME,
    SECURITY_REVIEW_FALLBACK_MARKER,
    _count_findings_in_review_file,
    _persist_security_review_artifact,
    run_security_review,
)
from equipa.security_gate import (
    MAX_REVIEW_ARTIFACT_BYTES,
    fingerprint_artifact,
    get_reviewer_run,
    read_artifact_text,
    reviewer_nonce_line,
    set_unrecorded_reviewer_runs_permitted,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BLOCKING_READ_DEADLINE_SECONDS = 10


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


def _lstat_reports_regular(monkeypatch, target: Path, regular: Path) -> None:
    """Make ``lstat(target)`` look like a regular file: the entry was swapped
    after the check. Every other path gets the real ``lstat``."""
    real_lstat = os.lstat
    disguise = real_lstat(regular)

    def swapped_lstat(path, *args, **kwargs):
        if os.fspath(path) == str(target):
            return disguise
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", swapped_lstat)


def _call_with_fifo_deadline(call: Callable[[], Any], fifo: Path) -> Any:
    """Run ``call`` in a thread; fail (and unblock it) if it hangs on ``fifo``.

    A reader blocked opening a FIFO is released by a non-blocking writer
    open, so a regression fails this test instead of hanging the suite.
    """
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = call()
        except BaseException as exc:  # re-raised in the test thread below
            outcome["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(BLOCKING_READ_DEADLINE_SECONDS)
    if worker.is_alive():
        with contextlib.suppress(OSError):
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(BLOCKING_READ_DEADLINE_SECONDS)
        pytest.fail(f"reading the artifact path {fifo} blocked on a FIFO")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


# ---------------------------------------------------------------------------
# SR41-01: backstops for an entry swapped in after lstat
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_fifo_swapped_in_after_lstat_is_refused_without_blocking(
    tmp_path, monkeypatch,
):
    """lstat saw a regular file, the path is now a FIFO: O_NONBLOCK keeps the
    open from hanging and the fstat re-check refuses it (an empty read of a
    writerless FIFO must not become a 0-byte "review")."""
    fifo = _artifact(tmp_path, 3301)
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)
    _lstat_reports_regular(monkeypatch, fifo, _write(tmp_path / "r.md", "x"))

    fingerprint = _call_with_fifo_deadline(
        lambda: fingerprint_artifact(fifo), fifo,
    )

    assert fingerprint.exists is False
    assert fingerprint.sha256 is None


def test_symlink_swapped_in_after_lstat_is_not_followed(tmp_path, monkeypatch):
    """lstat saw a regular file, the path is now a symlink: O_NOFOLLOW makes
    the open fail rather than read the link target."""
    real = _write(tmp_path / "elsewhere.md", _review_body(None, low=1))
    link = _artifact(tmp_path, 3302)
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    _lstat_reports_regular(monkeypatch, link, real)

    assert fingerprint_artifact(link).exists is False
    assert read_artifact_text(link) is None


def test_directory_at_artifact_path_is_missing(tmp_path):
    path = _artifact(tmp_path, 3303)
    path.mkdir(parents=True)

    assert fingerprint_artifact(path).exists is False
    assert _count_findings_in_review_file(path) is None


def test_artifact_exactly_at_size_cap_is_read(tmp_path):
    """The cap refuses LARGER than MAX_REVIEW_ARTIFACT_BYTES, not equal."""
    path = _artifact(tmp_path, 3304)
    path.parent.mkdir(parents=True)
    data = b"a" * MAX_REVIEW_ARTIFACT_BYTES
    path.write_bytes(data)

    fingerprint = fingerprint_artifact(path)

    assert fingerprint.exists is True
    assert fingerprint.size == MAX_REVIEW_ARTIFACT_BYTES
    assert fingerprint.sha256 == hashlib.sha256(data).hexdigest()


def test_non_utf8_artifact_hashes_raw_bytes_and_decodes_lossily(tmp_path):
    path = _artifact(tmp_path, 3305)
    path.parent.mkdir(parents=True)
    data = b"# Security Review\n\xff\xfe bad bytes\n"
    path.write_bytes(data)

    assert fingerprint_artifact(path).sha256 == hashlib.sha256(data).hexdigest()
    assert read_artifact_text(path) == data.decode("utf-8", errors="replace")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_gate_with_fifo_artifact_reports_missing_and_does_not_hang(tmp_path):
    """End to end through the merge gate, in a fresh process (production
    defaults, no hermetic opt-in). On 3923301 the gate logged
    artifact_exists=True and then blocked forever reading the FIFO."""
    fifo = _artifact(tmp_path, 3306)
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)
    probe = (
        "import sys;"
        "from equipa.dispatch import _security_review_blocks_merge;"
        "print(_security_review_blocks_merge(sys.argv[1], 3306,"
        " block_on_missing=False))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe, str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the merge gate blocked on a FIFO at the artifact path")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "(True, None)"
    eval_line = next(
        line for line in result.stderr.splitlines()
        if "event=blocks-merge-eval" in line
    )
    assert "artifact_exists=False" in eval_line
    assert "provenance=reviewer-record-missing" in eval_line


def test_symlinked_worktree_artifact_is_not_persisted(tmp_path):
    """A developer-planted symlink in the worktree must not smuggle its
    target's bytes to the stable path; the stable copy is a fallback dump,
    which the gate never trusts."""
    planted = _write(
        tmp_path / "outside" / "self-review.md",
        _review_body(None, low=1) + "PLANTED-SENTINEL\n",
    )
    worktree = tmp_path / "worktree"
    stable = tmp_path / "stable"
    link = _artifact(worktree, 3307)
    link.parent.mkdir(parents=True)
    link.symlink_to(planted)
    stable.mkdir()

    _persist_security_review_artifact(
        worktree_dir=str(worktree),
        stable_dir=str(stable),
        task_id=3307,
        result_text="raw reviewer stdout",
        agent_succeeded=True,
        output=[],
    )

    persisted = _artifact(stable, 3307)
    assert persisted.is_file() and not persisted.is_symlink()
    persisted_text = persisted.read_text(encoding="utf-8")
    assert "PLANTED-SENTINEL" not in persisted_text
    assert SECURITY_REVIEW_FALLBACK_MARKER in persisted_text
    assert _count_findings_in_review_file(persisted) is None


# ---------------------------------------------------------------------------
# SR41-02: counts come from the caller's bytes, not a second read
# ---------------------------------------------------------------------------


def test_count_with_text_never_reads_the_path(tmp_path):
    """``text=`` is the verified buffer; the path is only a label."""
    missing = tmp_path / "never-written.md"
    counts = _count_findings_in_review_file(
        missing, task_id=3308, text=_review_body(None, high=2, low=1),
    )
    assert counts is not None
    assert (counts["HIGH"], counts["LOW"]) == (2, 1)


def test_count_with_text_ignores_different_bytes_on_disk(tmp_path):
    path = _write(_artifact(tmp_path, 3309), _review_body(None, high=0, low=1))
    counts = _count_findings_in_review_file(
        path, task_id=3309, text=_review_body(None, high=3, low=0),
    )
    assert counts["HIGH"] == 3


# ---------------------------------------------------------------------------
# Reviewer harness (run_security_review with the agent layer scripted)
# ---------------------------------------------------------------------------


@pytest.fixture
def reviewer(monkeypatch):
    """Scripted reviewer: ``reviewer.attempts`` is a list of callables, one
    per attempt, each ``(task_id, nonce) -> sec_result``. Records the prompt
    and the RUNNING record seen at each attempt."""
    state = SimpleNamespace(
        task_id=None, attempts=[], prompts=[], running_records=[],
    )

    async def fake_run_agent(_cmd, timeout=None):
        running = get_reviewer_run(state.task_id)
        state.running_records.append(running)
        behaviour = state.attempts[len(state.running_records) - 1]
        return behaviour(state.task_id, running.nonce)

    @contextlib.contextmanager
    def fake_cli(*_args, **_kwargs):
        yield ["claude"]

    def fake_prompt(task, *_args, **_kwargs):
        # The description carries the attempt's nonce, so each attempt's
        # prompt (and its sha256) is distinct.
        prompt = f"reviewer prompt\n{task['description']}"
        state.prompts.append(prompt)
        return prompt

    async def no_diff(_project_dir):
        return 0

    monkeypatch.setattr(loops, "run_agent", fake_run_agent)
    monkeypatch.setattr(loops, "build_cli_command", fake_cli)
    monkeypatch.setattr(loops, "build_system_prompt", fake_prompt)
    monkeypatch.setattr(loops, "get_role_turns", lambda *a, **kw: 5)
    monkeypatch.setattr(loops, "get_role_model", lambda *a, **kw: "opus")
    monkeypatch.setattr(
        loops, "load_dispatch_config",
        lambda _p: {"security_review_timeout": 30},
    )
    monkeypatch.setattr(loops, "_measure_review_diff_lines", no_diff)
    monkeypatch.setattr(loops, "_extract_security_findings", lambda _t: [])
    return state


def _writes_review(project_dir: Path, *, high: int = 0):
    def behaviour(task_id, nonce):
        _write(_artifact(project_dir, task_id), _review_body(nonce, high=high))
        return {"success": True, "result_text": "done", "errors": []}
    return behaviour


def _times_out(_task_id, _nonce):
    return {"success": False, "result_text": "", "errors": ["timed out"]}


def _run(reviewer_state, project_dir: Path, task_id: int) -> list[str]:
    reviewer_state.task_id = task_id
    output: list[str] = []
    task = {"id": task_id, "title": "t", "description": "d", "project_id": 1}
    asyncio.run(run_security_review(
        task, str(project_dir), {}, SimpleNamespace(dispatch_config=None),
        output=output,
    ))
    return output


def test_run_security_review_counts_the_bytes_it_verified(
    tmp_path, reviewer, monkeypatch,
):
    """The reviewer's own counts line must not come from a second read:
    swap the file right after provenance passes and the line still reports
    the verified review's HIGH."""
    reviewer.attempts = [_writes_review(tmp_path, high=1)]
    path = _artifact(tmp_path, 3310)
    real_verify = loops.verify_reviewer_provenance
    swapped: list[str] = []

    def verify_then_swap(task_id, review_path):
        verdict = real_verify(task_id, review_path)
        if not swapped:
            swapped.append(verdict.fingerprint.sha256)
            path.write_text(_review_body(None, high=0), encoding="utf-8")
        return verdict

    monkeypatch.setattr(loops, "verify_reviewer_provenance", verify_then_swap)

    output = _run(reviewer, tmp_path, 3310)

    counts_line = next(line for line in output if "Security review counts" in line)
    assert "H=1" in counts_line, counts_line
    assert f"sha256={swapped[0][:16]}" in counts_line


def test_retry_keeps_one_run_id_and_hashes_the_last_prompt(
    tmp_path, reviewer, capsys,
):
    reviewer.attempts = [_times_out, _writes_review(tmp_path)]

    _run(reviewer, tmp_path, 3311)

    record = get_reviewer_run(3311)
    assert record.attempts == 2
    assert len(reviewer.prompts) == 2
    assert reviewer.prompts[0] != reviewer.prompts[1]
    # Every RUNNING record and the final record name the same reviewer run.
    assert {running.run_id for running in reviewer.running_records} == {
        record.run_id,
    }
    assert {running.model for running in reviewer.running_records} == {"opus"}
    assert record.prompt_sha256 == hashlib.sha256(
        reviewer.prompts[-1].encode("utf-8"),
    ).hexdigest()
    assert "event=reviewer-run " in capsys.readouterr().err


def test_run_ids_differ_between_reviewer_runs(tmp_path, reviewer):
    reviewer.attempts = [_writes_review(tmp_path), _writes_review(tmp_path)]

    _run(reviewer, tmp_path, 3312)
    first = get_reviewer_run(3312).run_id
    _run(reviewer, tmp_path, 3312)
    second = get_reviewer_run(3312).run_id

    assert first and second and first != second


def test_failed_reviewer_audit_line_names_the_reviewer(
    tmp_path, reviewer, capsys,
):
    reviewer.attempts = [_times_out, _times_out]

    _run(reviewer, tmp_path, 3313)

    record = get_reviewer_run(3313)
    assert record.run_id and record.prompt_sha256
    failed_line = next(
        line for line in capsys.readouterr().err.splitlines()
        if "event=reviewer-failed" in line
    )
    assert (
        f"reviewer_run={record.run_id} model=opus "
        f"prompt_sha256={record.prompt_sha256[:16]}"
    ) in failed_line


# ---------------------------------------------------------------------------
# SR41-03: the no-record verdict is labelled in the eval line
# ---------------------------------------------------------------------------


def test_no_record_eval_line_says_no_reviewer_ran(tmp_path, capsys):
    previous = set_unrecorded_reviewer_runs_permitted(False)
    try:
        _write(_artifact(tmp_path, 3314), _review_body(None, low=1))
        blocks, counts = _security_review_blocks_merge(str(tmp_path), 3314)
    finally:
        set_unrecorded_reviewer_runs_permitted(previous)

    assert (blocks, counts) == (True, None)
    eval_line = next(
        line for line in capsys.readouterr().err.splitlines()
        if "event=blocks-merge-eval" in line
    )
    assert "reviewer_run=none" in eval_line
    assert "provenance=reviewer-record-missing" in eval_line
    assert "artifact_exists=True" in eval_line
