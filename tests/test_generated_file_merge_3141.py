"""Task #3141 — follow-ups to the task #3131 generated-file merge resolution.

Each test runs the real gate and merge on real temporary git repositories and
fails on the task #3131 code:

* I-01: the generator's trust anchor is the guard's pinned default-branch SHA,
  never a re-read HEAD. The default branch moved to a commit carrying the
  branch's modified generator before the merge: the generator never runs.
  Moved mid-merge: nothing is committed on top of the moved branch. Without a
  pinned SHA no generator runs at all.
* I-02: the export is built from git objects, so ``export-ignore`` (in the
  branch's ``.gitattributes`` or in ``$GIT_DIR/info/attributes``) cannot drop
  a module from the regenerated report; links in the inputs are refused.
* I-03: a resolution commit that is not the verified tree is refused, and the
  guard checks the regenerated blob, not only the path.
* I-04: generator output is capped while it streams, the export is limited to
  the generator's inputs and bounded, and no tar archive is unpacked.

Every test runs with a throwaway HOME, so the operator's real global git
config is never read or written.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

import equipa.dispatch as dispatch_mod
import equipa.generated_files as generated_files_mod
from equipa.dispatch import _gated_merge_task, _merge_task_branch
from equipa.git_ops import reset_global_git_config_pin
from equipa.loops import ARTIFACTS_DIR_NAME
from equipa.merge_integrity import DefaultBranchGuard, MergeAttempt

TASK = 3141
BRANCH = f"forge-task-{TASK}"
REPORT = "equipa/MODULE_DEPENDENCY_REPORT.md"
GENERATOR = "scripts/gen_module_report.py"
REAL_GENERATOR = Path(__file__).resolve().parents[1] / GENERATOR


# --- git helpers -------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def _sha(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")


def _blob_of(repo: Path, content: str, *, write: bool = False) -> str:
    args = ["git", "hash-object", *(["-w"] if write else []), "--stdin"]
    return subprocess.run(
        args, cwd=str(repo), input=content, text=True, capture_output=True,
        check=True,
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit_all(cwd: Path, message: str) -> str:
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", message)
    return _sha(cwd, "HEAD")


def _run_real_generator(root: Path) -> None:
    subprocess.run(
        [sys.executable, str(root / GENERATOR)], cwd=str(root), check=True,
        capture_output=True,
    )


def _merge_head_exists(repo: Path) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"],
        cwd=str(repo), capture_output=True,
    ).returncode == 0


def _conflicted(repo: Path, ours: str, theirs: str) -> list[str]:
    result = subprocess.run(
        ["git", "merge-tree", "--write-tree", "--no-messages", "--name-only",
         ours, theirs],
        cwd=str(repo), text=True, capture_output=True,
    )
    return [line for line in result.stdout.splitlines()[1:] if line]


def _report_check(repo: Path) -> subprocess.CompletedProcess:
    """``gen_module_report.py --check`` on the main checkout (the CI check)."""
    return subprocess.run(
        [sys.executable, GENERATOR, "--check"], cwd=str(repo),
        capture_output=True, text=True,
    )


def _write_clean_review(repo: Path) -> None:
    """A clean artifact; the suite's conftest permits the unrecorded run."""
    _write(
        repo, f"{ARTIFACTS_DIR_NAME}/SECURITY-REVIEW-{TASK}.md",
        "# Security Review\n\nNo findings.\n\n## Counts\n"
        "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0\n",
    )


def _gate(repo: Path, worktree: Path) -> tuple[str, DefaultBranchGuard]:
    _write_clean_review(repo)
    guard = asyncio.run(DefaultBranchGuard.snapshot(repo))
    status = asyncio.run(_gated_merge_task(
        repo=repo, branch=BRANCH, outcome="tests_passed", task_id=TASK,
        guard=guard, worktree_dir=str(worktree),
    ))
    return status, guard


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, IndexError):
        return False
    return state != "Z"


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in (
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
        "GIT_ATTR_NOSYSTEM", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_global_git_config_pin()
    yield home
    reset_global_git_config_pin()


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@forgeborn.local")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    _write(path, ".gitignore", f"{ARTIFACTS_DIR_NAME}/\n.forge-worktrees/\n")
    _write(path, "equipa/__init__.py", "")
    _write(path, "equipa/core.py", "VALUE = 'base'\n")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project carrying the real generator and its committed report."""
    path = _init_repo(tmp_path / "repo")
    (path / "scripts").mkdir()
    shutil.copyfile(REAL_GENERATOR, path / GENERATOR)
    _run_real_generator(path)
    _commit_all(path, "base")
    return path


def _repo_with_generator(tmp_path: Path, generator_source: str) -> Path:
    """A project whose (trusted, unchanged) generator is ``generator_source``."""
    path = _init_repo(tmp_path / "repo")
    _write(path, GENERATOR, generator_source)
    _write(path, REPORT, "report v0\n")
    _commit_all(path, "base")
    return path


def _add_task_worktree(repo: Path) -> Path:
    worktree = repo / ".forge-worktrees" / f"task-{TASK}"
    _git(repo, "worktree", "add", "-q", "-b", BRANCH, str(worktree), "main")
    return worktree


def _diverge_with_report_conflict(
    repo: Path, branch_edit=None,
) -> tuple[Path, str, str]:
    """feature_b on the branch, feature_c on main, both regenerating the
    report. ``branch_edit(worktree)`` runs before the branch's generation.
    Returns (worktree, branch SHA, main SHA)."""
    worktree = _add_task_worktree(repo)
    _write(worktree, "equipa/feature_b.py", "from equipa import core\n")
    if branch_edit is not None:
        branch_edit(worktree)
    _run_real_generator(worktree)
    branch_sha = _commit_all(worktree, "feature b")
    _write(repo, "equipa/feature_c.py", "from equipa import core\n")
    _run_real_generator(repo)
    main_sha = _commit_all(repo, "feature c")
    assert _conflicted(repo, main_sha, branch_sha) == [REPORT]
    return worktree, branch_sha, main_sha


def _diverge_by_hand(repo: Path) -> tuple[Path, str, str]:
    worktree = _add_task_worktree(repo)
    _write(worktree, "equipa/feature_b.py", "B = 1\n")
    _write(worktree, REPORT, "report from the branch\n")
    branch_sha = _commit_all(worktree, "branch")
    _write(repo, "equipa/feature_c.py", "C = 1\n")
    _write(repo, REPORT, "report from main\n")
    main_sha = _commit_all(repo, "main")
    return worktree, branch_sha, main_sha


def _assert_failed_cleanly(
    repo: Path, guard: DefaultBranchGuard, main_sha: str, branch_sha: str,
) -> None:
    assert _sha(repo, "main") == main_sha
    assert _sha(repo, BRANCH) == branch_sha
    assert _git(repo, "status", "--porcelain") == ""
    assert not _merge_head_exists(repo)
    assert not guard.tripped
    assert guard.outcomes[TASK].merged_sha is None


def _hostile_generator(marker: Path) -> str:
    """A valid generator that proves it ran. (Prepending to the real script
    would put code before its ``from __future__`` import: a SyntaxError that
    never runs, which would make the marker check vacuous.)"""
    return (
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n"
        "print('report from the hostile generator')\n"
    )


def _commit_off_main(repo: Path, edits: dict[str, str], message: str) -> str:
    """A commit on top of main that main does not (yet) point at."""
    _git(repo, "checkout", "-q", "-b", "side")
    for rel, text in edits.items():
        _write(repo, rel, text)
    sha = _commit_all(repo, message)
    _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "-q", "-D", "side")
    return sha


# --- I-01: the generator trust anchor is the pinned SHA -----------------------


def test_default_branch_moved_before_the_merge_never_runs_the_generator(
    repo, tmp_path, monkeypatch, capsys,
):
    """The review's race: after the guard's pre-merge check, main is moved
    to a commit carrying the same modified generator as the branch. Compared
    with the re-read HEAD the generator looked unchanged and ran; compared
    with the pinned SHA it is refused, and the guard raises the alarm."""
    marker = tmp_path / "generator-ran"
    hostile = _hostile_generator(marker)
    worktree = _add_task_worktree(repo)
    _write(worktree, "equipa/feature_b.py", "from equipa import core\n")
    _run_real_generator(worktree)
    _write(worktree, GENERATOR, hostile)
    branch_sha = _commit_all(worktree, "feature b and a modified generator")
    _write(repo, "equipa/feature_c.py", "from equipa import core\n")
    _run_real_generator(repo)
    main_sha = _commit_all(repo, "feature c")
    moved = _commit_off_main(repo, {GENERATOR: hostile}, "agent moves main")
    assert _conflicted(repo, moved, branch_sha) == [REPORT]

    real_git = dispatch_mod.git_run_async
    moves = []

    async def move_main_before_pre_head(args, cwd, *rest, **kwargs):
        if list(args) == ["rev-parse", "HEAD"] and not moves:
            moves.append(cwd)
            _git(repo, "reset", "-q", "--hard", moved)
        return await real_git(args, cwd, *rest, **kwargs)

    monkeypatch.setattr(dispatch_mod, "git_run_async", move_main_before_pre_head)

    status, guard = _gate(repo, worktree)

    assert moves, "the race was not injected"
    assert not marker.exists(), "the branch-authored generator must never run"
    assert status == "blocked"
    assert guard.tripped and guard.outcomes[TASK].merged_sha is None
    assert _sha(repo, "main") == moved
    assert not _merge_head_exists(repo)
    assert _sha(repo, BRANCH) == branch_sha
    err = capsys.readouterr().err
    assert "event=generated-files-not-regenerated" in err
    assert f"pinned={main_sha}" in err
    assert "not the pinned default-branch SHA" in err


def test_default_branch_moved_mid_merge_is_not_committed_on(
    repo, monkeypatch, capsys,
):
    """Main moves while the checkout is mid-merge. The resolution refuses
    before anything runs; nothing is committed on top of the moved branch."""
    worktree, branch_sha, main_sha = _diverge_with_report_conflict(repo)
    moved = _commit_off_main(
        repo, {"equipa/feature_d.py": "D = 1\n"}, "agent moves main mid-merge",
    )

    real_git = dispatch_mod.git_run_async

    async def move_main_after_merge(args, cwd, *rest, **kwargs):
        result = await real_git(args, cwd, *rest, **kwargs)
        if args[:2] == ["merge", "--no-edit"]:
            _git(repo, "update-ref", "refs/heads/main", moved)
        return result

    monkeypatch.setattr(dispatch_mod, "git_run_async", move_main_after_merge)

    status, guard = _gate(repo, worktree)

    assert status == "blocked"
    assert guard.tripped and guard.outcomes[TASK].merged_sha is None
    assert _sha(repo, "main") == moved, "nothing may be committed on the moved branch"
    assert _sha(repo, BRANCH) == branch_sha
    assert main_sha != moved
    err = capsys.readouterr().err
    assert "event=generated-files-not-regenerated" in err
    assert f"not the pinned default-branch SHA {main_sha[:12]}" in err


def test_merge_without_a_pinned_sha_never_runs_a_generator(tmp_path):
    """No pinned anchor, no trusted generator: a direct merge without the
    guard's SHA keeps the ordinary conflict path."""
    marker = tmp_path / "generator-ran"
    repo = _repo_with_generator(tmp_path, (
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n"
        "print('fresh report')\n"
    ))
    worktree, branch_sha, main_sha = _diverge_by_hand(repo)
    _write_clean_review(repo)
    attempt = MergeAttempt()

    merged = asyncio.run(_merge_task_branch(
        str(repo), TASK, BRANCH, worktree_dir=str(worktree),
        merge_record=attempt,
    ))

    assert merged is False
    assert attempt.reason.startswith("merge and rebase conflict:")
    assert not marker.exists()
    assert _sha(repo, "main") == main_sha
    assert _sha(repo, BRANCH) == branch_sha
    assert not _merge_head_exists(repo)


# --- I-02: the export comes from git objects ----------------------------------


@pytest.mark.parametrize("source", ["branch-gitattributes", "info-attributes"])
def test_export_ignore_cannot_falsify_the_regenerated_report(repo, source):
    if source == "branch-gitattributes":
        def hide_core(worktree: Path) -> None:
            _write(worktree, ".gitattributes", "equipa/core.py export-ignore\n")
        worktree, _branch_sha, _main_sha = _diverge_with_report_conflict(
            repo, branch_edit=hide_core,
        )
    else:
        worktree, _branch_sha, _main_sha = _diverge_with_report_conflict(repo)
        info = Path(_git(repo, "rev-parse", "--git-path", "info/attributes"))
        if not info.is_absolute():
            info = repo / info
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_text(
            "equipa/core.py export-ignore\nequipa/feature_b.py export-ignore\n",
            encoding="utf-8",
        )

    status, guard = _gate(repo, worktree)

    assert status == "merged", guard.outcomes[TASK]
    check = _report_check(repo)
    assert check.returncode == 0, check.stdout + check.stderr
    report = (repo / REPORT).read_text(encoding="utf-8")
    for module in ("core.py", "feature_b.py", "feature_c.py"):
        assert f"| `{module}` |" in report, module


def test_symlink_in_the_generator_inputs_is_refused(repo, capsys):
    def plant_link(worktree: Path) -> None:
        os.symlink("core.py", worktree / "equipa" / "linked.py")

    worktree, branch_sha, main_sha = _diverge_with_report_conflict(
        repo, branch_edit=plant_link,
    )

    status, guard = _gate(repo, worktree)

    assert status == "merge_failed"
    reason = guard.outcomes[TASK].reason
    assert "'equipa/linked.py' is not a regular file (mode 120000)" in reason
    _assert_failed_cleanly(repo, guard, main_sha, branch_sha)
    assert "event=generated-files-not-regenerated" in capsys.readouterr().err


def test_regeneration_never_unpacks_a_tar_archive(repo, monkeypatch):
    """No dependency on tarfile's ``data`` filter (I-04): the export is
    written from git objects, so a tarfile that cannot be used is irrelevant."""
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("tarfile must not be used for the export")

    monkeypatch.setattr(tarfile, "open", unavailable)
    monkeypatch.setattr(tarfile.TarFile, "extractall", unavailable)
    worktree, _branch_sha, _main_sha = _diverge_with_report_conflict(repo)

    status, guard = _gate(repo, worktree)

    assert status == "merged", guard.outcomes[TASK]
    assert _report_check(repo).returncode == 0


# --- I-04: scoped, bounded export and streamed output cap ----------------------


def test_export_holds_only_the_generator_and_its_inputs(tmp_path):
    repo = _repo_with_generator(tmp_path, (
        "import pathlib, sys\n"
        "root = pathlib.Path(sys.argv[sys.argv.index('--repo-root') + 1])\n"
        "for path in sorted(root.rglob('*')):\n"
        "    if path.is_file():\n"
        "        print(path.relative_to(root).as_posix())\n"
    ))
    worktree = _add_task_worktree(repo)
    _write(worktree, "equipa/feature_b.py", "B = 1\n")
    _write(worktree, "docs/large.txt", "x" * 4096)
    _write(worktree, "sitecustomize.py", "raise SystemExit('ran')\n")
    _write(worktree, REPORT, "report from the branch\n")
    _commit_all(worktree, "branch")
    _write(repo, "equipa/feature_c.py", "C = 1\n")
    _write(repo, REPORT, "report from main\n")
    _commit_all(repo, "main")

    status, guard = _gate(repo, worktree)

    assert status == "merged", guard.outcomes[TASK]
    exported = (repo / REPORT).read_text(encoding="utf-8").split()
    assert exported == sorted([
        GENERATOR, "equipa/__init__.py", "equipa/core.py",
        "equipa/feature_b.py", "equipa/feature_c.py", REPORT,
    ])


def test_export_larger_than_the_cap_is_refused(tmp_path, monkeypatch):
    marker = tmp_path / "generator-ran"
    repo = _repo_with_generator(tmp_path, (
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n"
        "print('fresh report')\n"
    ))
    worktree, branch_sha, main_sha = _diverge_by_hand(repo)
    monkeypatch.setattr(generated_files_mod, "MAX_EXPORT_BYTES", 64, raising=False)

    status, guard = _gate(repo, worktree)

    assert status == "merge_failed"
    assert "inputs in the merged tree exceed 64 bytes" in guard.outcomes[TASK].reason
    assert not marker.exists()
    _assert_failed_cleanly(repo, guard, main_sha, branch_sha)


@pytest.mark.parametrize(
    ("stream", "cap_name", "expected"),
    [
        ("stdout", "MAX_GENERATED_BYTES", "output exceeds 1024 bytes"),
        ("stderr", "MAX_GENERATOR_STDERR_BYTES", "stderr exceeds 1024 bytes"),
    ],
)
def test_oversized_output_is_cut_off_while_streaming(
    tmp_path, monkeypatch, stream, cap_name, expected,
):
    """A generator that writes past the cap and keeps running is killed as
    soon as the cap is passed, not when the timeout fires."""
    pid_file = tmp_path / "generator.pid"
    repo = _repo_with_generator(tmp_path, (
        "import os, pathlib, sys, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        f"sys.{stream}.write('x' * 4096)\n"
        f"sys.{stream}.flush()\n"
        "time.sleep(120)\n"
    ))
    worktree, branch_sha, main_sha = _diverge_by_hand(repo)
    monkeypatch.setattr(generated_files_mod, cap_name, 1024, raising=False)
    monkeypatch.setattr(generated_files_mod, "GENERATOR_TIMEOUT_SECONDS", 30)

    started = time.monotonic()
    status, guard = _gate(repo, worktree)
    elapsed = time.monotonic() - started

    assert status == "merge_failed"
    assert expected in guard.outcomes[TASK].reason
    assert elapsed < 20, f"the cap waited for the timeout ({elapsed:.1f}s)"
    _assert_failed_cleanly(repo, guard, main_sha, branch_sha)
    assert not _alive(int(pid_file.read_text())), "the generator was not killed"


# --- I-03: the regenerated content is bound into the integrity check ----------


def test_index_swapped_before_the_commit_is_refused_not_merged(
    repo, monkeypatch, capsys,
):
    """A same-UID writer swaps the report in the index between write-tree
    and commit. The commit is not the verified tree: refused, and the guard
    raises the alarm on the moved branch instead of recording it."""
    worktree, branch_sha, _main_sha = _diverge_with_report_conflict(repo)
    evil = _blob_of(repo, "EVIL CONTENT\n", write=True)
    real_git = generated_files_mod.git_run_async

    async def swap_index_before_commit(args, cwd, *rest, **kwargs):
        if args and args[0] == "commit":
            _git(repo, "update-index", "--cacheinfo", f"100644,{evil},{REPORT}")
        return await real_git(args, cwd, *rest, **kwargs)

    monkeypatch.setattr(generated_files_mod, "git_run_async", swap_index_before_commit)

    status, guard = _gate(repo, worktree)

    assert status == "blocked"
    assert guard.tripped and guard.outcomes[TASK].merged_sha is None
    assert _sha(repo, BRANCH) == branch_sha
    assert "is not the verified tree" in capsys.readouterr().err


def _resolution_commit(repo: Path, main_sha: str, branch_sha: str, report: str) -> str:
    """A merge commit (main, branch) over git's merge with ``report`` as the
    report's content."""
    result = subprocess.run(
        ["git", "merge-tree", "--write-tree", "--no-messages", main_sha, branch_sha],
        cwd=str(repo), text=True, capture_output=True,
    )
    tree = result.stdout.splitlines()[0]
    index = repo.parent / "resolution.index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    subprocess.run(["git", "read-tree", tree], cwd=str(repo), env=env, check=True)
    blob = _blob_of(repo, report, write=True)
    subprocess.run(
        ["git", "update-index", "--cacheinfo", f"100644,{blob},{REPORT}"],
        cwd=str(repo), env=env, check=True,
    )
    new_tree = subprocess.run(
        ["git", "write-tree"], cwd=str(repo), env=env, text=True,
        capture_output=True, check=True,
    ).stdout.strip()
    index.unlink()
    return _git(repo, "commit-tree", new_tree, "-p", main_sha, "-p", branch_sha,
                "-m", "resolution")


@pytest.mark.parametrize(
    ("blobs_for", "accepted"),
    [
        ("committed", True),
        ("other", False),
        ("missing", False),
    ],
)
def test_guard_requires_the_verified_report_blob(repo, blobs_for, accepted):
    _worktree, branch_sha, main_sha = _diverge_with_report_conflict(repo)
    guard = asyncio.run(DefaultBranchGuard.snapshot(repo))
    resolution = _resolution_commit(repo, main_sha, branch_sha, "landed report\n")
    _git(repo, "update-ref", "refs/heads/main", resolution)
    blobs = {
        "committed": {REPORT: _blob_of(repo, "landed report\n")},
        "other": {REPORT: _blob_of(repo, "verified report\n")},
        "missing": None,
    }[blobs_for]
    kwargs = {} if blobs is None else {"regenerated_blobs": blobs}

    recorded = asyncio.run(guard.record_merge(
        TASK, branch_sha, post_head=resolution, regenerated_paths=(REPORT,),
        **kwargs,
    ))

    assert recorded is accepted
    assert guard.tripped is not accepted


def test_merged_resolution_records_the_verified_blob(repo, monkeypatch):
    """The gate hands the guard the blob of the exact generator output, and
    that is the blob the default branch carries."""
    seen: dict = {}
    real_record = DefaultBranchGuard.record_merge

    async def spy(self, task_id, merged_sha, **kwargs):
        seen.update(kwargs)
        return await real_record(self, task_id, merged_sha, **kwargs)

    monkeypatch.setattr(DefaultBranchGuard, "record_merge", spy)
    worktree, _branch_sha, _main_sha = _diverge_with_report_conflict(repo)

    status, guard = _gate(repo, worktree)

    assert status == "merged", guard.outcomes[TASK]
    landed = _git(repo, "rev-parse", f"main:{REPORT}")
    assert seen["regenerated_blobs"] == {REPORT: landed}
