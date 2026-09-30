"""gate-02 / gate-03: git_ops never honours refs/replace, hooks, or programs
named in agent-writable git config.

Every agent worktree shares the common git dir, so ``.git/config``,
``refs/replace/*`` and hook directories are agent-writable. These tests build
real temporary repositories, plant the attack with an UNHARDENED git (the
"agent"), prove the attack is live with a control run, and then assert the
``equipa.git_ops`` helpers are immune. No git call is mocked.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from equipa.git_ops import (
    create_task_worktree,
    git_run,
    git_run_async,
    merge_task_branch,
    remove_task_worktree,
)
from equipa.security_gate import get_changed_files_for_branch

# Every client-side hook a checkout, merge, rebase, stash or commit can fire.
# reference-transaction fires on every ref update, so it is the widest net.
_HOOK_NAMES = (
    "post-checkout",
    "post-merge",
    "pre-merge-commit",
    "pre-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "pre-rebase",
    "post-rewrite",
    "post-index-change",
    "reference-transaction",
)

_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "Probe",
    "GIT_AUTHOR_EMAIL": "probe@example.invalid",
    "GIT_COMMITTER_NAME": "Probe",
    "GIT_COMMITTER_EMAIL": "probe@example.invalid",
}


@pytest.fixture(autouse=True)
def _hermetic_git_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate every git call from the operator's global/system config.

    Identity comes from the environment, which also proves the hardened
    helpers keep the caller's env: without it the merges below cannot commit.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_EXTERNAL_DIFF", "GIT_EDITOR",
                 "GIT_SEQUENCE_EDITOR", "GIT_NO_REPLACE_OBJECTS", "GIT_DIR",
                 "GIT_WORK_TREE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in _IDENTITY_ENV.items():
        monkeypatch.setenv(name, value)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    """Plain, unhardened git: plays the agent and the control runs."""
    result = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        timeout=30, check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def _write_marker_script(path: Path, marker: Path, exit_code: int = 0) -> Path:
    """Executable shell script that records it ran by appending to ``marker``."""
    path.write_text(
        f'#!/bin/sh\necho "$0 $*" >> "{marker}"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


# --- gate-02: refs/replace ---------------------------------------------------


@pytest.fixture
def replaced_repo(tmp_path: Path) -> Path:
    """Branch ``forge-task-1`` adds evil.py; a replace ref disguises it.

    The code commit is replaced by a decoy commit that only edits README.md,
    so a git that honours refs/replace sees a doc-only change.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "checkout", "-q", "-b", "forge-task-1")
    evil_sha = _commit_file(repo, "evil.py", "import os\n", "add code")
    _git(repo, "checkout", "-q", "--detach", "main")
    decoy_sha = _commit_file(repo, "README.md", "base\ndocs\n", "docs only")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "replace", evil_sha, decoy_sha)
    # Control: an unhardened git is fooled, so the probe is live.
    assert _git(repo, "diff", "--name-only", "main...forge-task-1") == "README.md"
    return repo


@pytest.mark.asyncio
async def test_gate_diff_sees_real_files_despite_replace_ref(replaced_repo: Path) -> None:
    changed = await get_changed_files_for_branch(
        str(replaced_repo), base_ref="main", head_ref="forge-task-1",
    )
    assert changed == ["evil.py"]


@pytest.mark.asyncio
async def test_git_run_async_ignores_replace_refs(replaced_repo: Path) -> None:
    result = await git_run_async(
        ["diff", "--name-only", "main...forge-task-1"], replaced_repo, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["evil.py"]


def test_git_run_ignores_replace_refs(replaced_repo: Path) -> None:
    result = git_run(
        ["diff", "--name-only", "main...forge-task-1"], replaced_repo, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["evil.py"]


def test_merge_task_branch_merges_the_real_tree_despite_replace_ref(
    replaced_repo: Path,
) -> None:
    result = merge_task_branch(replaced_repo, 1, target_ref="main")
    assert result.returncode == 0, result.stderr
    assert (replaced_repo / "evil.py").is_file()
    assert (replaced_repo / "README.md").read_text(encoding="utf-8") == "base\n"


# --- gate-03: hooks from agent-written repo config ----------------------------


@pytest.fixture
def hooked_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Repo whose own config points core.hooksPath at marker-writing hooks.

    Returns ``(repo, marker)``. Branch ``forge-task-7`` adds feature.py.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "checkout", "-q", "-b", "forge-task-7")
    _commit_file(repo, "feature.py", "VALUE = 1\n", "feature")
    _git(repo, "checkout", "-q", "main")

    marker = tmp_path / "hook-ran.log"
    hooks_dir = tmp_path / "agent-hooks"
    hooks_dir.mkdir()
    for hook in _HOOK_NAMES:
        _write_marker_script(hooks_dir / hook, marker)
    _git(repo, "config", "core.hooksPath", str(hooks_dir))

    # Control: an unhardened checkout runs the agent's hooks.
    _git(repo, "checkout", "-q", "forge-task-7")
    _git(repo, "checkout", "-q", "main")
    assert marker.is_file(), "control: hooks should fire for plain git"
    marker.unlink()
    return repo, marker


def test_merge_task_branch_runs_no_repo_config_hooks(
    hooked_repo: tuple[Path, Path],
) -> None:
    repo, marker = hooked_repo
    _git(repo, "checkout", "-q", "forge-task-7")
    marker.unlink()

    result = merge_task_branch(repo, 7, target_ref="main")

    assert result.returncode == 0, result.stderr
    assert (repo / "feature.py").is_file()
    assert not marker.exists(), marker.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_git_run_async_checkout_merge_rebase_stash_run_no_hooks(
    hooked_repo: tuple[Path, Path],
) -> None:
    repo, marker = hooked_repo
    steps = (
        ["checkout", "-q", "forge-task-7"],
        ["checkout", "-q", "main"],
        ["merge", "--no-edit", "--no-ff", "forge-task-7"],
        ["checkout", "-q", "-b", "side", "main~1"],
        ["rebase", "main"],
        ["checkout", "-q", "main"],
    )
    for args in steps:
        result = await git_run_async(args, repo, timeout=30)
        assert result.returncode == 0, (args, result.stderr)

    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    for args in (["stash"], ["stash", "pop"]):
        result = await git_run_async(args, repo, timeout=30)
        assert result.returncode == 0, (args, result.stderr)

    assert (repo / "README.md").read_text(encoding="utf-8") == "dirty\n"
    assert not marker.exists(), marker.read_text(encoding="utf-8")


def test_task_worktree_lifecycle_runs_no_hooks(
    hooked_repo: tuple[Path, Path],
) -> None:
    repo, marker = hooked_repo
    worktree = create_task_worktree(repo, 9, base_ref="main")
    try:
        assert (worktree.path / "README.md").is_file()
    finally:
        remove_task_worktree(repo, worktree)
    assert not marker.exists(), marker.read_text(encoding="utf-8")


# --- other programs named in agent-writable config ----------------------------


@pytest.fixture
def plain_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _init_repo(repo)
    return repo


def _case_fsmonitor(repo: Path, script: Path) -> list[str]:
    _git(repo, "config", "core.fsmonitor", str(script))
    return ["status", "--porcelain"]


def _case_diff_external(repo: Path, script: Path) -> list[str]:
    _git(repo, "config", "diff.external", str(script))
    (repo / "README.md").write_text("base\nchanged\n", encoding="utf-8")
    return ["diff"]


def _case_editor(repo: Path, script: Path) -> list[str]:
    _git(repo, "config", "core.editor", str(script))
    return ["commit", "--allow-empty"]


def _case_commit_signing(repo: Path, script: Path) -> list[str]:
    _git(repo, "config", "commit.gpgSign", "true")
    _git(repo, "config", "gpg.program", str(script))
    return ["commit", "--allow-empty", "-m", "orchestrator commit"]


def _case_ext_transport(repo: Path, script: Path) -> list[str]:
    _git(repo, "config", "protocol.ext.allow", "always")
    return ["ls-remote", f"ext::{script}"]


_CONFIG_EXEC_CASES = {
    "core.fsmonitor": _case_fsmonitor,
    "diff.external": _case_diff_external,
    "core.editor": _case_editor,
    "commit.gpgSign+gpg.program": _case_commit_signing,
    "protocol.ext.allow": _case_ext_transport,
}


@pytest.mark.parametrize("case_name", sorted(_CONFIG_EXEC_CASES))
def test_repo_config_programs_never_run(
    plain_repo: Path, tmp_path: Path, case_name: str,
) -> None:
    marker = tmp_path / "agent-program-ran.log"
    script = _write_marker_script(tmp_path / "agent-program", marker, exit_code=1)
    args = _CONFIG_EXEC_CASES[case_name](plain_repo, script)

    # Control: an unhardened git runs the agent's program.
    subprocess.run(
        ["git", *args], cwd=str(plain_repo), capture_output=True, timeout=30,
        stdin=subprocess.DEVNULL, check=False,
    )
    assert marker.is_file(), f"control: plain git should run {case_name}"
    marker.unlink()

    git_run(args, plain_repo, timeout=30)

    assert not marker.exists(), marker.read_text(encoding="utf-8")


def test_diff_output_survives_neutralised_diff_external(
    plain_repo: Path, tmp_path: Path,
) -> None:
    """Pinning diff.external must not silently empty the diff the reviewer reads."""
    marker = tmp_path / "agent-program-ran.log"
    script = _write_marker_script(tmp_path / "agent-program", marker)
    args = _case_diff_external(plain_repo, script)

    result = git_run(args, plain_repo, timeout=30)

    assert result.returncode == 0, result.stderr
    assert "+changed" in result.stdout
    assert not marker.exists()


def test_commit_still_succeeds_when_agent_enables_signing(
    plain_repo: Path, tmp_path: Path,
) -> None:
    marker = tmp_path / "agent-program-ran.log"
    script = _write_marker_script(tmp_path / "agent-program", marker, exit_code=1)
    args = _case_commit_signing(plain_repo, script)

    result = git_run(args, plain_repo, timeout=30)

    assert result.returncode == 0, result.stderr
    assert _git(plain_repo, "log", "-1", "--format=%s") == "orchestrator commit"


def test_repo_ssh_command_is_replaced_by_plain_ssh_from_path(
    plain_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """core.sshCommand from repo config is ignored; ``ssh`` resolves on PATH.

    A stub ``ssh`` on PATH stands in for the operator's real ssh, so the test
    never touches the network, and shows the caller's PATH is kept.
    """
    agent_marker = tmp_path / "agent-ssh-ran.log"
    stub_marker = tmp_path / "path-ssh-ran.log"
    agent_script = _write_marker_script(tmp_path / "agent-ssh", agent_marker, 1)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_marker_script(bin_dir / "ssh", stub_marker, exit_code=1)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    _git(plain_repo, "config", "core.sshCommand", str(agent_script))
    args = ["ls-remote", "ssh://git.example.invalid/repo.git"]

    _git(plain_repo, *args, check=False)
    assert agent_marker.is_file(), "control: plain git should run core.sshCommand"
    agent_marker.unlink()

    git_run(args, plain_repo, timeout=30)

    assert not agent_marker.exists()
    assert stub_marker.is_file()


# --- environment handling -------------------------------------------------------


@pytest.mark.asyncio
async def test_caller_env_is_merged_and_hardening_cannot_be_unset(
    replaced_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EQUIPA_PROBE_INHERITED", "kept")
    result = await git_run_async(
        ["var", "GIT_AUTHOR_IDENT"], replaced_repo, timeout=10,
        env={"GIT_AUTHOR_NAME": "Caller Override"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Caller Override <probe@example.invalid>")

    # A caller cannot switch replace objects back on.
    diff = git_run(
        ["diff", "--name-only", "main...forge-task-1"], replaced_repo,
        timeout=10, env={"GIT_NO_REPLACE_OBJECTS": "0"},
    )
    assert diff.stdout.split() == ["evil.py"]


def test_completed_process_args_show_the_hardened_command(plain_repo: Path) -> None:
    result = git_run(["status", "--porcelain"], plain_repo, timeout=10)
    assert result.args[0] == "git"
    assert result.args[-2:] == ["status", "--porcelain"]
    assert "core.hooksPath=/dev/null" in result.args
