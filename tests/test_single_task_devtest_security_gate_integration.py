"""End-to-end integration tests for the single-task --dev-test security gate.

Task 2449 / SECURITY-REVIEW-2448 S2 (HIGH):
The existing unit tests in test_single_task_devtest_security_gate.py call
``dispatch._security_review_blocks_merge(...)`` directly. That helper PRE-DATES
the task 2448 patch (parallel mode already used it). The actual fix is the
orchestration glue in ``equipa/cli.py`` ``run_mode_task`` (~lines 1064-1171):

  * runs ``run_security_review`` BEFORE ``_post_task_telemetry``;
  * computes ``review_blocks_merge``;
  * demotes ``outcome`` to ``"security_review_blocked"``;
  * fail-closed on reviewer crash.

Without an integration test driving ``run_mode_task`` end-to-end, the suite
would still report all-pass even if the cli.py call site were deleted
(re-introducing the exact 2448 bug). This module closes that gap.

The load-bearing assertion is: when ``run_security_review`` reports >=1 HIGH,
the persisted task status is ``"security_review_blocked"`` (which is the
mechanism by which the merge gate prevents promotion — the outcome is what
``_post_task_telemetry`` writes to TheForge, and the unmerged branch is left
for operator review per the cli.py log lines at 1146-1158).

Verified by local revert experiment: temporarily removing the gate block at
cli.py:1073-1159 causes these tests to fail with ``outcome != "security_review_blocked"``
(it stays as ``"tests_passed"``) — proving the tests exercise the actual fix.
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

import equipa.cli as cli_mod
import equipa.dispatch as dispatch_mod


# ---------------------------------------------------------------------------
# Git helpers — small, surgical, no extra dependencies.
# ---------------------------------------------------------------------------

def _init_repo(path: Path) -> None:
    """Initialize a git repo with a master branch and one seed commit."""
    subprocess.run(["git", "init", "-b", "master"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forgeborn.dev"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=path, check=True, capture_output=True)


def _commit_files(path: Path, files: dict[str, str], message: str) -> None:
    """Commit ``files`` on whatever branch ``path`` has checked out.

    Task #3112 (dispatch-04): ``run_mode_task`` now creates the
    ``forge-task-<id>`` worktree itself, so the fake Dev+Test loop commits
    the agent's work inside the directory it is handed — exactly where a
    real agent works. A pre-existing branch would be refused as stale.
    """
    for rel, content in files.items():
        fp = path / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=path, check=True, capture_output=True)


def _master_sha(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "master"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _branch_merged_to_master(path: Path, branch: str) -> bool:
    """True iff ``branch`` has been merged into master (via git branch --merged)."""
    result = subprocess.run(
        ["git", "branch", "--merged", "master"], cwd=path, check=True, capture_output=True, text=True
    )
    return any(line.strip().lstrip("* ").strip() == branch for line in result.stdout.splitlines())


# ---------------------------------------------------------------------------
# run_mode_task driver — heavy monkeypatching of cli-module dependencies.
# ---------------------------------------------------------------------------

def _build_args(task_id: int, project_dir: Path) -> argparse.Namespace:
    """Build a Namespace with all attributes ``run_mode_task`` needs."""
    return argparse.Namespace(
        task=task_id,
        project=None,
        role="developer",
        dev_test=True,
        dry_run=False,
        yes=True,
        retries=0,
        dispatch_config={},
        security_review=True,
    )


def _drive_run_mode_task(
    *,
    repo: Path,
    task_id: int,
    review_high: int,
    review_critical: int,
    review_crash: bool,
    doc_only: bool,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str],
) -> dict[str, Any]:
    """Drive ``equipa.cli.run_mode_task`` with controlled monkeypatches.

    Captures the ``outcome`` argument that ``_post_task_telemetry`` receives —
    which is the value that gets persisted to TheForge via
    ``update_task_status``. That outcome is the load-bearing signal: if the
    gate fix is in place, a HIGH finding demotes it to
    ``"security_review_blocked"`` BEFORE telemetry persists it.
    """
    fake_task = {
        "id": task_id,
        "title": "test task",
        "description": "test description",
        "priority": "high",
        "project_name": "TestProject",
        "project_id": 9999,
        "role": "developer",
    }
    captured: dict[str, Any] = {"outcome": None, "calls": []}

    monkeypatch.setattr(cli_mod, "fetch_task", lambda _id: fake_task)
    monkeypatch.setattr(cli_mod, "fetch_next_todo", lambda _proj: fake_task)
    monkeypatch.setattr(cli_mod, "resolve_project_dir", lambda _t: str(repo))
    monkeypatch.setattr(cli_mod, "fetch_project_context", lambda _pid: {})
    monkeypatch.setattr(cli_mod, "_auto_snapshot_dispatch", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "is_security_review_enabled", lambda *a, **kw: True)
    monkeypatch.setattr(cli_mod, "get_task_complexity", lambda _t: "medium")
    monkeypatch.setattr(cli_mod, "get_role_model", lambda *a, **kw: "claude-test")
    monkeypatch.setattr(cli_mod, "get_role_turns", lambda *a, **kw: 50)
    monkeypatch.setattr(cli_mod, "calculate_dynamic_budget", lambda turns, **kw: (turns, turns))
    monkeypatch.setattr(cli_mod, "load_checkpoint", lambda *a, **kw: (None, None))
    monkeypatch.setattr(cli_mod, "verify_task_updated", lambda _id: (True, "ok"))
    monkeypatch.setattr(cli_mod, "print_dev_test_summary", lambda *a, **kw: None)

    captured["dev_dirs"] = []

    async def fake_dev_test_loop(task, project_dir, project_context, args, output=None):
        captured["dev_dirs"].append(project_dir)
        _commit_files(Path(project_dir), files, f"feat: task {task['id']}")
        return ({"tests_passed": 1}, 1, "tests_passed")

    monkeypatch.setattr(cli_mod, "run_dev_test_loop", fake_dev_test_loop)

    # The review now runs in equipa.dispatch.review_task_branch (shared by
    # every isolated mode), against the REAL diff of the task branch —
    # doc-only-ness is derived from the files the fake agent committed.
    async def fake_security_review(
        task, project_dir, project_context, args, output=None,
        stable_project_dir=None,
    ):
        captured["review_dirs"] = (project_dir, stable_project_dir)
        if review_crash:
            raise RuntimeError("simulated reviewer crash")
        body_lines = ["# Security Review\n", "## Findings\n"]
        for i in range(review_critical):
            body_lines.append(f"### [C{i+1}] CRITICAL — simulated\n\nDetails...\n")
        for i in range(review_high):
            body_lines.append(f"### [H{i+1}] HIGH — simulated\n\nDetails...\n")
        if review_high == 0 and review_critical == 0:
            body_lines.append("### [M1] MEDIUM — cosmetic\n\nDetails...\n")
        (repo / f"SECURITY-REVIEW-{task['id']}.md").write_text("".join(body_lines), encoding="utf-8")

    monkeypatch.setattr(dispatch_mod, "run_security_review", fake_security_review)

    async def fake_telemetry(task, result, outcome, *a, **kw):
        captured["outcome"] = outcome
        captured["calls"].append({"task_id": task["id"], "outcome": outcome})

    monkeypatch.setattr(cli_mod, "_post_task_telemetry", fake_telemetry)

    args = _build_args(task_id, repo)
    asyncio.run(cli_mod.run_mode_task(args))
    return captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSingleTaskDevtestSecurityGateIntegration:
    """End-to-end coverage of the run_mode_task security-gate orchestration.

    These tests intentionally drive ``equipa.cli.run_mode_task`` (the call
    site of the task-2448 patch) rather than ``_security_review_blocks_merge``
    (the helper, which pre-dates the patch). Removing the gate block at
    cli.py:1073-1159 must cause them to fail — that is the contract the
    unit-level helper tests cannot enforce.
    """

    def test_high_severity_finding_demotes_outcome(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the review reports >=1 HIGH, outcome must be demoted to
        ``security_review_blocked`` (so the branch is left unmerged for
        operator review per cli.py:1146-1158)."""
        _init_repo(tmp_path)
        master_before = _master_sha(tmp_path)

        captured = _drive_run_mode_task(
            repo=tmp_path, task_id=99, review_high=1, review_critical=0,
            review_crash=False, doc_only=False, monkeypatch=monkeypatch,
            files={"src/foo.py": "def f(): return 1\n"},
        )

        assert captured["outcome"] == "security_review_blocked", (
            "HIGH findings must demote outcome — if this fails, the gate at "
            "cli.py:1073-1159 has regressed (task 2448 bug re-introduced)"
        )
        assert _master_sha(tmp_path) == master_before
        assert not _branch_merged_to_master(tmp_path, "forge-task-99")

    def test_critical_severity_finding_demotes_outcome(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CRITICAL findings must demote outcome identically to HIGH."""
        _init_repo(tmp_path)

        captured = _drive_run_mode_task(
            repo=tmp_path, task_id=100, review_high=0, review_critical=1,
            review_crash=False, doc_only=False, monkeypatch=monkeypatch,
            files={"src/bar.py": "def g(): return 2\n"},
        )

        assert captured["outcome"] == "security_review_blocked"
        assert not _branch_merged_to_master(tmp_path, "forge-task-100")

    def test_clean_review_preserves_success_outcome(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clean review (0 CRITICAL, 0 HIGH) must NOT demote the outcome."""
        _init_repo(tmp_path)

        captured = _drive_run_mode_task(
            repo=tmp_path, task_id=101, review_high=0, review_critical=0,
            review_crash=False, doc_only=False, monkeypatch=monkeypatch,
            files={"src/baz.py": "def h(): return 3\n"},
        )

        assert captured["outcome"] == "tests_passed", (
            "clean review must not block — outcome should remain tests_passed"
        )
        # Task #3112: the agent worked in the forge-task worktree, the
        # reviewer read that worktree, and the work reached master through
        # the gated merge (the merged branch and its worktree are removed).
        worktree = tmp_path / ".forge-worktrees" / "task-101"
        assert captured["dev_dirs"] == [str(worktree)]
        assert captured["review_dirs"] == (str(worktree), str(tmp_path))
        assert (tmp_path / "src" / "baz.py").read_text() == "def h(): return 3\n"
        assert not worktree.exists()

    def test_reviewer_crash_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If ``run_security_review`` raises, the gate must fail closed
        (demote outcome to ``security_review_blocked``).

        This is the task 2341 S2 parity case: a crashed reviewer must not
        be allowed to silently re-authorise the merge based on a stale
        artifact from a prior run.
        """
        _init_repo(tmp_path)

        captured = _drive_run_mode_task(
            repo=tmp_path, task_id=102, review_high=0, review_critical=0,
            review_crash=True, doc_only=False, monkeypatch=monkeypatch,
            files={"src/qux.py": "def i(): return 4\n"},
        )

        assert captured["outcome"] == "security_review_blocked"

    def test_doc_only_diff_skips_gate_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A doc-only diff (.md/.txt/.rst only) must skip the gate entirely
        and allow the success outcome through (task 2360 parity).

        This complements the doc-only unit test in
        test_single_task_devtest_security_gate.py by proving the
        short-circuit is wired into the orchestration glue, not just the
        helper.
        """
        _init_repo(tmp_path)

        captured = _drive_run_mode_task(
            repo=tmp_path, task_id=103, review_high=99, review_critical=99,
            review_crash=False, doc_only=True, monkeypatch=monkeypatch,
            files={"docs/x.md": "# notes\n"},
        )

        # Even with simulated review_high=99, the doc-only short-circuit
        # must prevent run_security_review from being called and the
        # outcome must NOT be demoted.
        assert captured["outcome"] == "tests_passed", (
            "doc-only diff must skip gate — if this fails, the doc-only "
            "short-circuit at cli.py:1088-1117 has regressed"
        )
        # And: no SECURITY-REVIEW-*.md artifact should have been written
        # (because run_security_review was never called).
        assert not (tmp_path / "SECURITY-REVIEW-103.md").exists(), (
            "doc-only short-circuit must prevent run_security_review from running"
        )
