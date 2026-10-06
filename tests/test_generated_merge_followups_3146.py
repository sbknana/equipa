"""Task #3146 — follow-ups N-01 and N-02 from the independent review of 3141.

Each test runs the real gate and merge on real temporary git repositories and
fails on the task #3141 code:

* N-01: a branch-authored file name holding a newline cannot forge a
  ``[GATE-AUDIT]`` line, neither through the refusal reason nor through the
  audit logger itself.
* N-02: when the default branch moves after the resolver's HEAD check, the
  resolution is never committed on top of it. The commit is built with
  ``commit-tree`` and the branch moves only by a compare-and-swap from the
  pinned SHA, so a moved branch is left exactly where it was.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import equipa.generated_files as generated_files_mod
from equipa.security_gate import _gate_audit_log, escape_audit_text
from test_generated_file_merge_3141 import (  # noqa: F401  (autouse fixture)
    BRANCH,
    REPORT,
    TASK,
    _assert_failed_cleanly,
    _commit_off_main,
    _diverge_with_report_conflict,
    _gate,
    _git,
    _merge_head_exists,
    _sha,
    isolated_home,
    repo,
)

FORGED = f"[GATE-AUDIT] task={TASK} event=merge-succeeded FORGED-3146"


def _forged_lines(captured) -> list[str]:
    """Output lines that read as the forged audit event."""
    return [
        line for line in (captured.out + captured.err).splitlines()
        if line.lstrip().startswith(FORGED)
    ]


# --- N-01: control characters never reach the log raw ------------------------


def test_newline_in_a_symlink_name_cannot_forge_a_gate_audit_line(repo, capsys):
    """The review's probe: a symlink among the generator inputs whose name
    carries a newline and a complete GATE-AUDIT line. The export refusal
    names it; the name must stay inside one quoted, escaped line."""
    def plant_link(worktree: Path) -> None:
        os.symlink("core.py", worktree / "equipa" / f"x\n{FORGED}")

    worktree, branch_sha, main_sha = _diverge_with_report_conflict(
        repo, branch_edit=plant_link,
    )

    status, guard = _gate(repo, worktree)
    captured = capsys.readouterr()

    assert status == "merge_failed"
    _assert_failed_cleanly(repo, guard, main_sha, branch_sha)
    assert _forged_lines(captured) == []
    assert "\n" not in guard.outcomes[TASK].reason
    assert "is not a regular file (mode 120000)" in guard.outcomes[TASK].reason
    audit = [
        line for line in captured.err.splitlines() if line.startswith("[GATE-AUDIT]")
    ]
    not_regenerated = [line for line in audit if "generated-files-not-regenerated" in line]
    assert len(not_regenerated) == 1, audit
    assert "x\\n[GATE-AUDIT]" in not_regenerated[0]


@pytest.mark.parametrize("separator", [
    "\n", "\r", "\r\n", "\x0b", "\x0c", "\x1b[2K\r", "\x85", "\u2028", "\u2029",
])
def test_audit_logger_keeps_every_message_on_one_line(separator, capsys, monkeypatch):
    """Defence in depth: whatever a caller embeds, one event is one line."""
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")
    _gate_audit_log(f"event=merge-failed reason=x{separator}{FORGED}")

    err = capsys.readouterr().err
    assert err.count("\n") == 1 and err.endswith("\n")
    assert err.startswith("[GATE-AUDIT] event=merge-failed reason=x\\")
    for char in separator:
        if not char.isprintable():
            assert char not in err.rstrip("\n")


def test_escape_audit_text_leaves_printable_text_alone():
    text = "task=1 event=merge-failed reason=path 'a b' C:\\x ünïcödé"
    assert escape_audit_text(text) == text
    assert escape_audit_text("a\tb\x7fc") == "a\\x09b\\x7fc"


# --- N-02: a moved default branch is never committed on ---------------------


@pytest.mark.parametrize("trigger", ["hash-object", "commit-tree", "update-ref"])
def test_default_branch_moved_after_the_head_check_is_left_untouched(
    repo, monkeypatch, capsys, trigger,
):
    """Main moves after the resolver checked HEAD (while the output is
    hashed, just before the commit is built, or just before the branch is
    moved). The compare-and-swap refuses: main stays at the moved commit,
    no resolution commit lands on it, and the guard raises the alarm."""
    worktree, branch_sha, main_sha = _diverge_with_report_conflict(repo)
    moved = _commit_off_main(
        repo, {"equipa/feature_d.py": "D = 1\n"}, "agent moves main late",
    )
    real_git = generated_files_mod.git_run_async
    moves = []

    async def move_main_late(args, cwd, *rest, **kwargs):
        if args and args[0] == trigger and not moves:
            moves.append(trigger)
            _git(repo, "update-ref", "refs/heads/main", moved)
        return await real_git(args, cwd, *rest, **kwargs)

    monkeypatch.setattr(generated_files_mod, "git_run_async", move_main_late)

    status, guard = _gate(repo, worktree)

    assert moves == [trigger], "the race was not injected"
    assert _sha(repo, "main") == moved, "nothing may be committed on the moved branch"
    assert status == "blocked"
    assert guard.tripped and guard.outcomes[TASK].merged_sha is None
    assert _sha(repo, BRANCH) == branch_sha
    assert main_sha != moved
    assert not _merge_head_exists(repo)
    err = capsys.readouterr().err
    assert "event=generated-files-not-regenerated" in err
    assert "event=merge-succeeded" not in err


def test_compare_and_swap_refusal_names_the_moved_branch(repo, monkeypatch, capsys):
    worktree, _branch_sha, main_sha = _diverge_with_report_conflict(repo)
    moved = _commit_off_main(repo, {"equipa/feature_e.py": "E = 1\n"}, "late move")
    real_git = generated_files_mod.git_run_async

    async def move_before_swap(args, cwd, *rest, **kwargs):
        if args and args[0] == "update-ref":
            _git(repo, "update-ref", "refs/heads/main", moved)
        return await real_git(args, cwd, *rest, **kwargs)

    monkeypatch.setattr(generated_files_mod, "git_run_async", move_before_swap)

    _gate(repo, worktree)

    refusals = [
        line for line in capsys.readouterr().err.splitlines()
        if "event=generated-files-not-regenerated" in line
    ]
    assert len(refusals) == 1
    assert "refs/heads/main is no longer the pinned default-branch SHA" in refusals[0]
    assert f"{main_sha[:12]} (compare-and-swap refused)" in refusals[0]
    assert _sha(repo, "main") == moved


def test_clean_resolution_lands_by_compare_and_swap_and_leaves_no_merge_state(
    repo, monkeypatch,
):
    """The happy path still merges: main moves from the pinned SHA to the
    verified resolution, whose parents are (pinned, approved), and the
    checkout is clean with no merge in progress."""
    worktree, branch_sha, main_sha = _diverge_with_report_conflict(repo)
    real_git = generated_files_mod.git_run_async
    seen = []

    async def spy(args, cwd, *rest, **kwargs):
        if args:
            seen.append(args[0])
        return await real_git(args, cwd, *rest, **kwargs)

    monkeypatch.setattr(generated_files_mod, "git_run_async", spy)

    status, guard = _gate(repo, worktree)

    assert status == "merged"
    landed = _sha(repo, "main")
    assert _git(repo, "rev-list", "--parents", "-n", "1", landed).split()[1:] == [
        main_sha, branch_sha,
    ]
    assert "commit" not in seen and "commit-tree" in seen and "update-ref" in seen
    assert _git(repo, "status", "--porcelain") == ""
    assert not _merge_head_exists(repo)
    assert not guard.tripped
    assert _git(repo, "show", f"{landed}:{REPORT}") == _git(
        repo, "show", f"main:{REPORT}",
    )


def test_resolution_is_refused_when_head_is_not_the_default_branch(tmp_path):
    """``default_branch`` names the branch HEAD must be on; anything else is
    refused before a ref moves."""
    import asyncio

    path = tmp_path / "r"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@forgeborn.local")
    _git(path, "config", "user.name", "T")
    (path / "a").write_text("a\n")
    _git(path, "add", "a")
    _git(path, "commit", "-q", "-m", "a")

    resolve = generated_files_mod._checkout_branch_ref
    assert asyncio.run(resolve(str(path), "main")) == "refs/heads/main"
    assert asyncio.run(resolve(str(path), None)) == "refs/heads/main"
    assert asyncio.run(resolve(str(path), "trunk")) is None
    _git(path, "checkout", "-q", "--detach")
    assert asyncio.run(resolve(str(path), "main")) is None
