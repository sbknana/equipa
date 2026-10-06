"""Task #3176 (FF-3173) — the refusal store and the central non-git guard.

IR73-01: the N1 check (task #3173) records a repository found in a project
that was not git at dispatch, and every later dispatch of a project that
would discover it is refused. The records were put next to
``Path(THEFORGE_DB).absolute()``: a symlinked database put them next to the
link, a symlinked store (or one others can write) took them wherever it
pointed, and with THEFORGE_DB unset the store was an untracked folder in the
EQUIPA checkout, which the deploy script's inventory refuses. The store now
follows the resolved database, is refused unless it is a real directory of
this user that no one else can write, and the in-checkout fallback is
listed in ``.gitignore``.

IR73-02: the "no git in a project that was not git at dispatch" record was
asked for at each call site, so a helper that did not ask ran git, and the
agent's filters, there (S3168-01 was such a helper). The process runners of
``equipa.git_ops`` now refuse it themselves; the tests below call git the way
a new helper would, without asking.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import equipa.dispatch as dispatch_mod
import equipa.git_ops as git_ops
from equipa.git_ops import git_run, git_run_async
from equipa.monitoring import dispatched_without_git

from test_dispatch_modes_gated_3112 import _init_repo
from test_non_git_dispatch_3168 import _planted_project
from test_worktree_poison_matrix_3158 import CHANGE_CHECK_VECTOR, GitRecorder

REPO_ROOT = Path(__file__).resolve().parent.parent
TASK_ID = 3176

# git's own "not a git repository" status, which the refusal reports.
NOT_A_REPOSITORY = 128


# --- IR73-01: where the records are kept ------------------------------------------


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The GATE-AUDIT lines the N1 check writes."""
    lines: list[str] = []
    monkeypatch.setattr(
        dispatch_mod, "log_gate_audit", lambda line, *args, **kwargs: lines.append(line),
    )
    return lines


def _use_database(monkeypatch: pytest.MonkeyPatch, database: Path) -> None:
    monkeypatch.setattr(dispatch_mod._equipa_constants, "THEFORGE_DB", database)


def _project_with_repository(tmp_path: Path, name: str = "project") -> Path:
    """A project the N1 check finds a repository in."""
    project = tmp_path / name
    (project / ".git").mkdir(parents=True)
    return project


def _real_database(tmp_path: Path) -> Path:
    database = tmp_path / "real" / "operator.db"
    database.parent.mkdir()
    database.write_bytes(b"")
    return database


def _database_is_a_symlink(tmp_path: Path) -> Path:
    real = _real_database(tmp_path)
    links = tmp_path / "links"
    links.mkdir()
    (links / "operator.db").symlink_to(real)
    return links / "operator.db"


def _database_is_under_a_symlink(tmp_path: Path) -> Path:
    real = _real_database(tmp_path)
    (tmp_path / "links").symlink_to(real.parent, target_is_directory=True)
    return tmp_path / "links" / "operator.db"


@pytest.mark.parametrize(
    "make_database", [_database_is_a_symlink, _database_is_under_a_symlink],
    ids=["database-symlink", "directory-symlink"],
)
def test_the_records_are_kept_next_to_the_real_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit: list[str], make_database: Any,
) -> None:
    database = make_database(tmp_path)
    _use_database(monkeypatch, database)
    project = _project_with_repository(tmp_path)
    real_store = Path(os.path.realpath(tmp_path / "real")) / (
        dispatch_mod.AGENT_REPOSITORY_REFUSALS_DIRNAME
    )

    assert dispatch_mod._agent_repository_refusals_dir() == real_store
    assert dispatch_mod._repository_appeared_in_non_git_project(
        TASK_ID, str(project), "after attempt 1", [],
    )

    assert sorted(path.suffix for path in real_store.iterdir()) == [".json"]
    assert not os.path.lexists(
        tmp_path / "links" / dispatch_mod.AGENT_REPOSITORY_REFUSALS_DIRNAME
    ) or os.path.realpath(
        tmp_path / "links" / dispatch_mod.AGENT_REPOSITORY_REFUSALS_DIRNAME
    ) == str(real_store)
    assert "later dispatches there are refused until" in audit[0]
    with pytest.raises(dispatch_mod.AgentMadeRepositoryError):
        dispatch_mod.refuse_agent_made_repository(str(project))


def _store_links_to_a_directory(store: Path, elsewhere: Path) -> None:
    store.symlink_to(elsewhere, target_is_directory=True)


def _store_is_a_dangling_link(store: Path, elsewhere: Path) -> None:
    store.symlink_to(elsewhere / "missing", target_is_directory=True)


def _store_is_a_file(store: Path, elsewhere: Path) -> None:
    store.write_text("not a directory\n")


def _store_is_writable_by_everyone(store: Path, elsewhere: Path) -> None:
    store.mkdir()
    store.chmod(0o777)


def _store_is_writable_by_the_group(store: Path, elsewhere: Path) -> None:
    store.mkdir()
    store.chmod(0o770)


UNTRUSTED_STORES = {
    "symlink-to-directory": (_store_links_to_a_directory, "is a symlink"),
    "dangling-symlink": (_store_is_a_dangling_link, "is a symlink"),
    "regular-file": (_store_is_a_file, "is not a directory"),
    "world-writable": (_store_is_writable_by_everyone, "writable by other users"),
    "group-writable": (_store_is_writable_by_the_group, "writable by other users"),
}


@pytest.mark.parametrize("shape", sorted(UNTRUSTED_STORES))
def test_a_store_that_cannot_be_trusted_refuses_every_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit: list[str], shape: str,
) -> None:
    """Fail closed: nothing is written through the store, the task that found
    the repository is still blocked, and every project is refused, not only
    the one whose record could not be written."""
    database = _real_database(tmp_path)
    _use_database(monkeypatch, database)
    store = database.parent / dispatch_mod.AGENT_REPOSITORY_REFUSALS_DIRNAME
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    make_store, reason = UNTRUSTED_STORES[shape]
    make_store(store, elsewhere)
    project = _project_with_repository(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()

    assert dispatch_mod._repository_appeared_in_non_git_project(
        TASK_ID, str(project), "after attempt 1", [],
    )

    assert sorted(elsewhere.iterdir()) == []
    assert "the refusal of later dispatches could NOT be recorded" in audit[0]
    for directory in (project, unrelated):
        with pytest.raises(dispatch_mod.AgentMadeRepositoryError) as refused:
            dispatch_mod.refuse_agent_made_repository(str(directory))
        assert reason in str(refused.value)
        assert "Refusing to run git there" in str(refused.value)


def _windows_reported_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: Any,
) -> Path:
    """A store as Windows reports it: no ``os.geteuid``, and a writable
    directory reads mode 0o777 (CPython derives it from the read-only
    attribute alone)."""
    database = _real_database(tmp_path)
    _use_database(monkeypatch, database)
    store = database.parent / dispatch_mod.AGENT_REPOSITORY_REFUSALS_DIRNAME
    store.mkdir()
    store.chmod(0o777)
    monkeypatch.delattr(dispatch_mod.os, "geteuid")
    monkeypatch.setattr(dispatch_mod, "_owned_by_this_windows_user", owner,
                        raising=False)
    return store


def test_on_windows_a_store_of_this_user_is_trusted_whatever_its_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IR76-04 (task #3178): the mode test refused every Windows store, so
    after the first N1 block every dispatch was refused. Windows reads the
    owner from the security descriptor instead."""
    asked: list[Path] = []
    store = _windows_reported_store(
        tmp_path, monkeypatch, lambda path: asked.append(path) or True)

    dispatch_mod._check_refusal_store(store)

    assert asked == [store]


@pytest.mark.parametrize("owner_reading", ["another-owner", "unreadable"])
def test_on_windows_a_store_not_shown_to_be_this_users_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_reading: str,
) -> None:
    def owner(path: Path) -> bool:
        if owner_reading == "unreadable":
            raise OSError(5, "access denied")
        return False

    store = _windows_reported_store(tmp_path, monkeypatch, owner)

    with pytest.raises(dispatch_mod.RefusalStoreError) as refused:
        dispatch_mod._check_refusal_store(store)
    assert ("is not owned by this user" if owner_reading == "another-owner"
            else "its owner cannot be read") in str(refused.value)


def test_the_windows_owner_check_fails_closed_without_the_windows_api(
    tmp_path: Path,
) -> None:
    if hasattr(__import__("ctypes"), "WinDLL"):
        pytest.fail("this host has the Windows API; run the other Windows tests")
    with pytest.raises(OSError, match="no Windows security API"):
        dispatch_mod._owned_by_this_windows_user(tmp_path)


def test_a_posix_store_writable_by_others_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: on POSIX the owner check does not replace the mode check."""
    database = _real_database(tmp_path)
    _use_database(monkeypatch, database)
    store = database.parent / dispatch_mod.AGENT_REPOSITORY_REFUSALS_DIRNAME
    store.mkdir()
    store.chmod(0o777)
    monkeypatch.setattr(dispatch_mod, "_owned_by_this_windows_user",
                        lambda path: True, raising=False)

    with pytest.raises(dispatch_mod.RefusalStoreError, match="writable by other users"):
        dispatch_mod._check_refusal_store(store)


RELATIVE_DATABASE_PROBE = """
import os, sys
from pathlib import Path
import equipa.dispatch as dispatch
before = dispatch._agent_repository_refusals_dir()
os.chdir(sys.argv[1])
after = dispatch._agent_repository_refusals_dir()
print(before)
print(after)
"""


def test_a_relative_database_keeps_its_store_after_a_chdir(tmp_path: Path) -> None:
    """IR76-08 (task #3178): THEFORGE_DB is made absolute once, at import,
    so a relative value keeps naming the same database and refusal store
    after the process changes directory (a recorded refusal then read as
    "allowed")."""
    started_in = tmp_path / "started"
    (started_in / "forge").mkdir(parents=True)
    moved_to = tmp_path / "moved"
    moved_to.mkdir()
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("DATABASE_URL", "PGPASSFILE")}
    environment["THEFORGE_DB"] = os.path.join("forge", "theforge.db")
    environment["PYTHONPATH"] = str(REPO_ROOT)

    probe = subprocess.run(
        [sys.executable, "-c", RELATIVE_DATABASE_PROBE, str(moved_to)],
        cwd=started_in, env=environment, capture_output=True, text=True,
        timeout=120, check=False,
    )

    assert probe.returncode == 0, probe.stderr
    expected = Path(os.path.realpath(started_in / "forge")) / (
        dispatch_mod.AGENT_REPOSITORY_REFUSALS_DIRNAME)
    assert probe.stdout.split("\n")[:2] == [str(expected), str(expected)]


def test_a_store_of_this_user_is_used_and_kept_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit: list[str],
) -> None:
    """Control: the store EQUIPA makes passes its own checks."""
    database = _real_database(tmp_path)
    _use_database(monkeypatch, database)
    store = database.parent / dispatch_mod.AGENT_REPOSITORY_REFUSALS_DIRNAME
    project = _project_with_repository(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()

    assert dispatch_mod._agent_made_repository_record(str(project)) is None
    assert not os.path.lexists(store), "looking a record up made the store"

    assert dispatch_mod._repository_appeared_in_non_git_project(
        TASK_ID, str(project), "after attempt 1", [],
    )

    assert store.stat().st_mode & 0o077 == 0
    assert "later dispatches there are refused until" in audit[0]
    with pytest.raises(dispatch_mod.AgentMadeRepositoryError) as refused:
        dispatch_mod.refuse_agent_made_repository(str(project / "src"))
    assert f"delete {store}" in str(refused.value)
    dispatch_mod.refuse_agent_made_repository(str(unrelated))


def test_the_in_checkout_store_is_listed_in_gitignore() -> None:
    entries = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert f"/{dispatch_mod.AGENT_REPOSITORY_REFUSALS_DIRNAME}/" in entries


def test_the_deploy_inventory_does_not_see_the_in_checkout_store(tmp_path: Path) -> None:
    """``scripts/deploy-equipa-prod.sh`` refuses to deploy over any untracked
    path it does not know (``git status --porcelain``). A checkout with the
    repository's .gitignore and a refusal record in the default store lists
    nothing there."""
    checkout = _init_repo(tmp_path / "checkout")
    shutil.copyfile(REPO_ROOT / ".gitignore", checkout / ".gitignore")
    subprocess.run(["git", "add", ".gitignore"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "-m", "gitignore"],
        cwd=checkout, check=True,
    )
    store = checkout / dispatch_mod.AGENT_REPOSITORY_REFUSALS_DIRNAME
    store.mkdir(mode=0o700)
    (store / f"{'0' * 64}.json").write_text("{}\n")

    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=checkout, capture_output=True, text=True, check=True,
    )

    assert status.stdout == ""


# --- IR73-02: no git in a project that was not git, whoever asks -------------------


def _forgetful_sync(project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    return git_run(["diff", "--stat"], project)


def _forgetful_status_below(project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    (project / "src").mkdir(exist_ok=True)
    return git_run(["status", "--short"], project / "src")


def _forgetful_through_a_symlink(project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    link = tmp_path / "project-link"
    link.symlink_to(project, target_is_directory=True)
    return git_run(["diff", "--stat"], link)


def _forgetful_change_directory(project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    return git_run(["-C", "project", "diff", "--stat"], tmp_path)


def _forgetful_git_dir_option(project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    return git_run(
        [f"--git-dir={project / '.git'}", f"--work-tree={project}", "diff", "--stat"],
        tmp_path,
    )


def _forgetful_git_dir_variable(project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    return git_run(
        ["diff", "--stat"], tmp_path,
        env={"GIT_DIR": str(project / ".git"), "GIT_WORK_TREE": str(project)},
    )


def _another_repository_with_a_change(tmp_path: Path) -> None:
    other = _init_repo(tmp_path / "other")
    (other / "README.md").write_text("SEED\n")


def _forgetful_git_common_dir_variable(
    project: Path, tmp_path: Path,
) -> subprocess.CompletedProcess:
    """git run in a repository of its own outside the project still reads
    config and info/attributes, so the agent's filter, from GIT_COMMON_DIR."""
    return git_run(
        ["diff", "--stat"], tmp_path / "other",
        env={"GIT_COMMON_DIR": str(project / ".git")},
    )


def _forgetful_async(project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    return asyncio.run(git_run_async(["diff", "--stat"], project))


def _forgetful_process_runner(project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    """A helper that builds its own argv, as the private git dir does."""
    env = git_ops._get_repo_env()
    return asyncio.run(git_ops._run_git_process_async(
        git_ops._hardened_git_argv(["diff", "--stat"], env), str(project), env, 30,
    ))


def _elsewhere(tmp_path: Path) -> Path:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    return elsewhere


def _forgetful_relative_git_dir_before_change_directory(
    project: Path, tmp_path: Path,
) -> subprocess.CompletedProcess:
    """IR76-01: git resolves a relative --git-dir / --work-tree against the
    directory the LAST -C leaves, not the one current where it is given."""
    return git_run(
        ["--git-dir=project/.git", "--work-tree=project", "-C", str(tmp_path),
         "diff", "--stat"],
        _elsewhere(tmp_path),
    )


def _forgetful_relative_git_dir_variable_after_change_directory(
    project: Path, tmp_path: Path,
) -> subprocess.CompletedProcess:
    return git_run(
        ["-C", str(tmp_path), "diff", "--stat"], _elsewhere(tmp_path),
        env={"GIT_DIR": "project/.git", "GIT_WORK_TREE": "project"},
    )


def _forgetful_env_wrapper(project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    """IR76-09: the guard reads -C only when the program is git itself."""
    return git_ops._run_with_env(
        ["env", "git", "-C", str(project), "diff", "--stat"], _elsewhere(tmp_path), 30,
    )


def _forgetful_shell_wrapper(project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    return git_ops._run_with_env(
        ["sh", "-c", "cd project && git diff --stat"], tmp_path, 30,
    )


def _forgetful_timeout_wrapper(project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    return git_ops._run_with_env(
        ["/usr/bin/timeout", "30", "git", "diff", "--stat"], _elsewhere(tmp_path), 30,
        env={"GIT_DIR": str(project / ".git"), "GIT_WORK_TREE": str(project)},
    )


def _forgetful_async_wrapper(project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    env = git_ops._get_repo_env()
    return asyncio.run(git_ops._run_git_process_async(
        ["env", f"GIT_DIR={project / '.git'}", f"GIT_WORK_TREE={project}",
         "git", "diff", "--stat"],
        str(_elsewhere(tmp_path)), env, 30,
    ))


# IR78-04 (task #3180): a location carried by config, a repository named to
# a transport subcommand, and an alias (a wrapper inside git).
def _forgetful_config(*config: str, env: dict[str, str] | None = None,
                      subcommand: tuple[str, ...] = ("status", "--short")):
    def helper(project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
        values = [part.format(project=project) for part in config]
        variables = {key: value.format(project=project)
                     for key, value in (env or {}).items()}
        return git_run(
            [*values, *(part.format(project=project) for part in subcommand)],
            _elsewhere(tmp_path), env=variables or None,
        )

    return helper


IR78_04_HELPERS = {
    "git_run-c-core.worktree": _forgetful_config("-c", "core.worktree={project}"),
    "git_run-config-env-core.worktree": _forgetful_config(
        "--config-env=core.worktree=PLANTED_WORK_TREE",
        env={"PLANTED_WORK_TREE": "{project}"}),
    "git_run-GIT_CONFIG_COUNT-core.worktree": _forgetful_config(env={
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.worktree",
        "GIT_CONFIG_VALUE_0": "{project}"}),
    "git_run-GIT_CONFIG_PARAMETERS-core.worktree": _forgetful_config(
        env={"GIT_CONFIG_PARAMETERS": "'core.worktree'='{project}'"}),
    "git_run-GIT_CONFIG_GLOBAL-in-the-project": _forgetful_config(
        env={"GIT_CONFIG_GLOBAL": "{project}/.git/config"}),
    "git_run-fetch-the-project": _forgetful_config(subcommand=("fetch", "{project}")),
    "git_run-fetch-a-file-url": _forgetful_config(
        subcommand=("fetch", "file://{project}/.git")),
    "git_run-ls-remote-localhost": _forgetful_config(
        subcommand=("ls-remote", "localhost:{project}")),
    "git_run-clone-the-project": _forgetful_config(
        subcommand=("clone", "--no-checkout", "{project}", "{project}-copy")),
    "git_run-remote-url-config": _forgetful_config(
        "-c", "remote.planted.url={project}", subcommand=("fetch", "planted")),
    "git_run-url-insteadOf": _forgetful_config(
        "-c", "url.{project}.insteadOf=planted:", subcommand=("fetch", "planted:x")),
    "git_run-shell-alias": _forgetful_config(
        "-c", "alias.z=!cd {project} && git diff --stat", subcommand=("z",)),
    "git_run-GIT_CONFIG_COUNT-alias": _forgetful_config(
        env={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "alias.z",
             "GIT_CONFIG_VALUE_0": "!git -C {project} diff"},
        subcommand=("z",)),
    "git_run-upload-pack-wrapper": _forgetful_config(subcommand=(
        "ls-remote", "--upload-pack=sh -c 'cd {project} && git diff' #", "/tmp")),
    "git_run-unreadable-GIT_CONFIG_COUNT": _forgetful_config(
        env={"GIT_CONFIG_COUNT": "many"}),
}


FORGETFUL_HELPERS = {
    **IR78_04_HELPERS,
    "git_run": _forgetful_sync,
    "git_run-relative-git-dir-before-dash-C":
        _forgetful_relative_git_dir_before_change_directory,
    "git_run-relative-GIT_DIR-after-dash-C":
        _forgetful_relative_git_dir_variable_after_change_directory,
    "process-runner-env-wrapper": _forgetful_env_wrapper,
    "process-runner-sh-wrapper": _forgetful_shell_wrapper,
    "process-runner-timeout-wrapper": _forgetful_timeout_wrapper,
    "async-process-runner-env-wrapper": _forgetful_async_wrapper,
    "git_run-below-the-project": _forgetful_status_below,
    "git_run-through-a-symlink": _forgetful_through_a_symlink,
    "git_run-dash-C": _forgetful_change_directory,
    "git_run-git-dir-option": _forgetful_git_dir_option,
    "git_run-GIT_DIR": _forgetful_git_dir_variable,
    "git_run-GIT_COMMON_DIR": _forgetful_git_common_dir_variable,
    "git_run_async": _forgetful_async,
    "process-runner": _forgetful_process_runner,
}

# Set up before git is recorded: the test's own git is not the helper's.
FORGETFUL_HELPER_PREPARATIONS = {
    "git_run-GIT_COMMON_DIR": _another_repository_with_a_change,
}


@pytest.mark.parametrize("helper", sorted(FORGETFUL_HELPERS))
def test_a_helper_that_does_not_ask_still_runs_no_git_in_a_non_git_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper: str,
) -> None:
    project, agent = _planted_project(tmp_path, monkeypatch)
    prepare = FORGETFUL_HELPER_PREPARATIONS.get(helper)
    if prepare is not None:
        prepare(tmp_path)
    recorder = GitRecorder(monkeypatch)

    with dispatched_without_git(project), recorder.recording():
        result = FORGETFUL_HELPERS[helper](project, tmp_path)

    assert agent.ran() == [], f"EXECUTED {CHANGE_CHECK_VECTOR}/{helper}: {agent.ran()}"
    assert recorder.calls == [], [entry.argv for entry in recorder.calls]
    assert result.returncode == NOT_A_REPOSITORY
    assert result.stdout == ""
    assert "not a git repository at dispatch" in result.stderr


IR78_04_ALLOWED = {
    "config-elsewhere": ["git", "-c", "core.worktree={elsewhere}", "status"],
    "config-of-no-location": ["git", "-c", "user.name=t", "-c", "core.hooksPath=", "log"],
    "fetch-a-remote-name": ["git", "fetch", "origin"],
    "fetch-another-repository": ["git", "fetch", "{elsewhere}"],
    "push-set-upstream": ["git", "push", "-u", "origin", "main"],
    "diff-of-a-path-named-like-the-project": ["git", "diff", "--", "project"],
}


@pytest.mark.parametrize("shape", sorted(IR78_04_ALLOWED))
def test_config_and_transport_shapes_elsewhere_are_allowed(tmp_path: Path, shape: str) -> None:
    """Control for IR78-04: only a location in the recorded project (or an
    alias, or a remote-side program) is refused, never config or transport
    as such."""
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = _elsewhere(tmp_path)
    argv = [part.format(elsewhere=elsewhere) for part in IR78_04_ALLOWED[shape]]
    with dispatched_without_git(project):
        assert git_ops._non_git_project_refusal(argv, elsewhere, {}) is None


def test_a_wrapper_runs_while_no_project_is_recorded(tmp_path: Path) -> None:
    """IR76-09 refuses programs other than git and gh only during a
    dispatch that recorded a project as not git."""
    result = git_ops._run_with_env(["sh", "-c", "echo ran"], tmp_path, 30)
    assert (result.returncode, result.stdout) == (0, "ran\n")


def test_the_guard_reads_the_options_of_a_windows_git_executable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    windows_git = ["C:/Program Files/Git/cmd/git.EXE", "-C", str(project), "status"]
    with dispatched_without_git(project):
        refusal = git_ops._non_git_project_refusal(windows_git, tmp_path, {})
        allowed = git_ops._non_git_project_refusal(
            [windows_git[0], "status"], tmp_path, {})
    assert refusal is not None and f"at {project}" in refusal
    assert allowed is None


def _gh_in_the_project(project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    return git_ops._gh_run(["pr", "list"], cwd=str(project))


def _gh_given_the_projects_git_dir(
    project: Path, tmp_path: Path,
) -> subprocess.CompletedProcess:
    """A helper that starts gh elsewhere with its own environment: gh passes
    GIT_DIR on to the git it runs."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    return git_ops._run_with_env(
        ["gh", "pr", "list"], elsewhere, 30, {"GIT_DIR": str(project / ".git")},
    )


GH_HELPERS = {
    "gh-in-the-project": _gh_in_the_project,
    "gh-given-GIT_DIR": _gh_given_the_projects_git_dir,
}


@pytest.mark.parametrize("helper", sorted(GH_HELPERS))
def test_gh_is_not_started_in_a_non_git_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper: str,
) -> None:
    """gh runs git by discovery too."""
    project = tmp_path / "project"
    project.mkdir()
    started: list[Any] = []
    monkeypatch.setattr(
        subprocess, "run", lambda argv, *args, **kwargs: started.append(argv),
    )

    with dispatched_without_git(project):
        result = GH_HELPERS[helper](project, tmp_path)

    assert started == []
    assert result.returncode == NOT_A_REPOSITORY
    assert "not a git repository at dispatch" in result.stderr


def test_git_still_runs_outside_the_recorded_project(tmp_path: Path) -> None:
    """Control: the record covers the project, not a sibling whose name it
    prefixes, and not a git project of the same dispatch."""
    project = tmp_path / "project"
    project.mkdir()
    sibling = _init_repo(tmp_path / "project2")

    with dispatched_without_git(project):
        result = git_run(["rev-parse", "--is-inside-work-tree"], sibling)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "true"


def test_without_the_record_git_runs_in_the_same_directory(tmp_path: Path) -> None:
    """Control: the refusal comes from the record, not from the directory."""
    repo = _init_repo(tmp_path / "project")

    with dispatched_without_git(tmp_path / "another-project"):
        inside = git_run(["rev-parse", "--is-inside-work-tree"], repo)

    assert inside.returncode == 0, inside.stderr
    with dispatched_without_git(repo):
        refused = git_run(["rev-parse", "--is-inside-work-tree"], repo)
    assert refused.returncode == NOT_A_REPOSITORY
    after = git_run(["rev-parse", "--is-inside-work-tree"], repo)
    assert after.returncode == 0, after.stderr


def test_one_record_is_read_by_the_guard_and_the_change_checks() -> None:
    """monitoring re-exports the git_ops record, so a dispatch's record is
    the one the process runners read."""
    import equipa.monitoring as monitoring

    assert monitoring.dispatched_without_git is git_ops.dispatched_without_git
    assert monitoring.git_checks_allowed is git_ops.git_checks_allowed


# --- IR73-02: the git_ops runners are the only place git is started ---------------

# The guard is in the git_ops runners, so a helper that started git or gh
# itself would still run it in a project that was not git. These tests read
# the orchestrator's source (as a syntax tree: a comment or docstring that
# mentions git satisfies nothing) for any other place git or gh is started.
GIT_PROGRAMS = frozenset({"git", "gh", "git.exe", "gh.exe"})
RUNNERS_MODULE = REPO_ROOT / "equipa" / "git_ops.py"

# The calls that start a program named by one of their positional arguments
# (an argv, a program, or a shell command line).
PROCESS_STARTERS = frozenset({
    *(f"subprocess.{name}" for name in (
        "run", "Popen", "call", "check_call", "check_output",
        "getoutput", "getstatusoutput",
    )),
    *(f"{module}.{name}" for module in ("asyncio", "asyncio.subprocess")
      for name in ("create_subprocess_exec", "create_subprocess_shell")),
    *(f"os.{name}" for name in (
        "system", "popen", "posix_spawn", "posix_spawnp",
        "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe",
        "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp",
        "spawnlpe",
    )),
    # IR78-03 (task #3180).
    "pty.spawn",
})
# Event-loop methods, called on a loop object whose name the source does
# not fix.
LOOP_PROCESS_STARTERS = frozenset({"subprocess_exec", "subprocess_shell"})
# The modules a starter is an attribute of: ``getattr(subprocess, name)``
# with a name the fence cannot resolve may be any of their starters.
PROCESS_MODULES = frozenset(starter.rsplit(".", 1)[0] for starter in PROCESS_STARTERS)
# A starter the fence cannot name (an unresolved ``getattr`` of a process
# module, a container of different starters): its start is unproven.
UNRESOLVED_STARTER = "<a starter the fence cannot name>"
# List methods that add items to an argv a name holds (IR78-03:
# ``cmd = ["nice"]; cmd.extend(["git", "diff"])``).
ARGV_MUTATORS = frozenset({"extend", "append", "insert"})


def _imported_names(tree: ast.Module) -> dict[str, str]:
    """Each name a module binds by an absolute import, mapped to what it
    names (``from subprocess import run as start`` binds start to
    subprocess.run)."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    names[head] = head
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _called_name(function: ast.expr, imported: dict[str, str]) -> str | None:
    """The dotted name a call's function resolves to, through the module's
    imports; None for a call of anything but a (dotted) name."""
    parts: list[str] = []
    while isinstance(function, ast.Attribute):
        parts.append(function.attr)
        function = function.value
    if not isinstance(function, ast.Name):
        return parts[0] if parts else None
    parts.append(imported.get(function.id, function.id))
    return ".".join(reversed(parts))


# Programs that start another program their own arguments name: ``env git
# -C P``, ``sh -c "cd P && git diff"``, ``python3 -c "..."``. Such a start
# is proven not to start git only when every later argument is a known
# string holding no git word (IR76-02, task #3178).
WRAPPER_PROGRAMS = frozenset({
    "env", "sh", "bash", "dash", "zsh", "ksh", "fish", "busybox", "cmd", "cmd.exe",
    "powershell", "powershell.exe", "pwsh", "timeout", "nice", "nohup", "ionice",
    "chrt", "taskset", "setsid", "stdbuf", "sudo", "doas", "su", "runuser", "xargs",
    "flock", "script", "systemd-run", "unshare", "nsenter", "bwrap", "firejail",
    "strace", "ltrace", "time", "watch", "parallel", "python", "python3", "uv", "uvx",
})
# The stand-in for a value the fence cannot resolve to a string.
UNKNOWN = None
# Where each starter takes its program and arguments: the positional index
# of the argv (a list), of the program (a string), of the first of the
# separate argument strings, or of a shell command line.
_ARGV, _PROGRAM, _REST, _COMMAND_LINE = "argv", "program", "rest", "command line"
STARTER_ARGUMENTS: dict[str, tuple[tuple[str, int], ...]] = {
    **{f"subprocess.{name}": ((_ARGV, 0),)
       for name in ("run", "Popen", "call", "check_call", "check_output")},
    **{f"subprocess.{name}": ((_COMMAND_LINE, 0),)
       for name in ("getoutput", "getstatusoutput")},
    **{f"{module}.create_subprocess_exec": ((_PROGRAM, 0), (_REST, 1))
       for module in ("asyncio", "asyncio.subprocess")},
    **{f"{module}.create_subprocess_shell": ((_COMMAND_LINE, 0),)
       for module in ("asyncio", "asyncio.subprocess")},
    "os.system": ((_COMMAND_LINE, 0),),
    "os.popen": ((_COMMAND_LINE, 0),),
    "os.posix_spawn": ((_PROGRAM, 0), (_ARGV, 1)),
    "os.posix_spawnp": ((_PROGRAM, 0), (_ARGV, 1)),
    **{f"os.{name}": ((_PROGRAM, 0), (_ARGV, 1))
       for name in ("execv", "execve", "execvp", "execvpe")},
    **{f"os.{name}": ((_PROGRAM, 0), (_REST, 1))
       for name in ("execl", "execle", "execlp", "execlpe")},
    **{f"os.{name}": ((_PROGRAM, 1), (_ARGV, 2))
       for name in ("spawnv", "spawnve", "spawnvp", "spawnvpe")},
    **{f"os.{name}": ((_PROGRAM, 1), (_REST, 2))
       for name in ("spawnl", "spawnle", "spawnlp", "spawnlpe")},
    "subprocess_exec": ((_PROGRAM, 1), (_REST, 2)),
    "subprocess_shell": ((_COMMAND_LINE, 1),),
    "pty.spawn": ((_ARGV, 0),),
}
# Keyword arguments that hand a starter its argv or its program.
_ARGV_KEYWORDS = frozenset({"args"})
_PROGRAM_KEYWORDS = frozenset({"executable", "path", "file", "program"})
_SHELL_WORD_SEPARATORS = re.compile(r"[\s;&|()<>`$'\"=]+")


MAX_ARGVS = 64


def _combined(heads: list[list[str | None]],
              tails: list[list[str | None]]) -> list[list[str | None]]:
    """Every head followed by every tail; past ``MAX_ARGVS`` combinations
    the rest is one argv of unknown content (fail closed)."""
    combined = [head + tail for head in heads for tail in tails]
    if len(combined) > MAX_ARGVS:
        return combined[:MAX_ARGVS] + [[UNKNOWN]]
    return combined


def _names_git_word(text: str) -> bool:
    """Whether any shell word of ``text`` is git or gh (``cd P && git``)."""
    return any(os.path.basename(word) in GIT_PROGRAMS
               for word in _SHELL_WORD_SEPARATORS.split(text) if word)


@dataclass(frozen=True)
class ProcessStart:
    """One call that starts a process, and what the fence proved of it."""

    line: int
    function: str
    called: str
    verdict: str            # "git", "unproven" or "not git"

    def describe(self, filename: str) -> str:
        what = ("starts git" if self.verdict == "git"
                else "starts a program the fence cannot prove is not git or gh")
        return f"{filename}:{self.line}: {self.called} in {self.function} {what}"


class _ProcessStartFence:
    """Classifies each process start of a module: its program is git or gh,
    cannot be proven not to be ("unproven", fail closed), or is proven not
    to be. Values are followed through the names the module binds."""

    def __init__(self, tree: ast.Module, imported: dict[str, str]) -> None:
        self._imported = imported
        self._bindings: dict[str, list[ast.expr]] = {}
        # Items added to the argv a name holds, in source order: (True when
        # they may land first, the items as an argv node).
        self._additions: dict[str, list[tuple[bool, ast.expr]]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                self._note_argv_mutation(node)
                continue
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value:
                targets, value = [node.target], node.value
                if (isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add)
                        and isinstance(node.target, ast.Name)):
                    self._additions.setdefault(node.target.id, []).append((False, value))
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    self._bindings.setdefault(target.id, []).append(value)
        self._resolving: set[str] = set()

    def _note_argv_mutation(self, call: ast.Call) -> None:
        """Record ``name.extend(items)``, ``name.append(item)`` and
        ``name.insert(index, item)`` as items added to ``name``'s argv; an
        insert at index 0, or at an index the source does not fix, may put
        its item first."""
        function = call.func
        if not (isinstance(function, ast.Attribute) and function.attr in ARGV_MUTATORS
                and isinstance(function.value, ast.Name) and call.args):
            return
        name = function.value.id
        if function.attr == "extend":
            self._additions.setdefault(name, []).append((False, call.args[0]))
        elif function.attr == "append":
            self._additions.setdefault(name, []).append(
                (False, ast.List(elts=[call.args[0]], ctx=ast.Load())))
        elif len(call.args) >= 2:
            index = call.args[0]
            later = (isinstance(index, ast.Constant) and isinstance(index.value, int)
                     and not isinstance(index.value, bool) and index.value > 0)
            self._additions.setdefault(name, []).append(
                (not later, ast.List(elts=[call.args[1]], ctx=ast.Load())))

    # --- what a call starts -------------------------------------------------------

    def starter(self, call: ast.Call) -> tuple[str, list[ast.expr], dict[str, ast.expr]] | None:
        """The starter ``call`` runs and the arguments it hands it; through
        ``functools.partial`` and names bound to a starter too."""
        called = self._starter_name(call.func)
        if called is not None:
            return called, list(call.args), {k.arg: k.value for k in call.keywords if k.arg}
        if (_called_name(call.func, self._imported) in ("functools.partial", "partial")
                and call.args):
            called = self._starter_name(call.args[0])
            if called is not None:
                return (called, list(call.args[1:]),
                        {k.arg: k.value for k in call.keywords if k.arg})
        return None

    def _starter_name(self, function: ast.expr, depth: int = 0) -> str | None:
        called = _called_name(function, self._imported)
        if called in PROCESS_STARTERS:
            return called
        if called is not None and called.rsplit(".", 1)[-1] in LOOP_PROCESS_STARTERS:
            return called.rsplit(".", 1)[-1]
        if depth >= 8:
            return None
        if isinstance(function, ast.Name):
            for value in self._bindings.get(function.id, []):
                found = self._starter_name(value, depth + 1)
                if found is not None:
                    return found
        if isinstance(function, ast.Call):
            return self._starter_got(function)
        if isinstance(function, ast.Subscript):
            # ``STARTERS["run"](...)``: any starter the container holds.
            found = {name for element in self._elements(function.value, depth)
                     if (name := self._starter_name(element, depth + 1)) is not None}
            if len(found) == 1:
                return found.pop()
            if found:
                return UNRESOLVED_STARTER
        return None

    def _starter_got(self, call: ast.Call) -> str | None:
        """The starter ``getattr(<process module>, name)`` gets, an
        unresolved one when the name is not known, or the one a
        container's ``.get`` returns (IR78-03)."""
        if isinstance(call.func, ast.Attribute) and call.func.attr == "get":
            container = call.func.value
            found = {name for element in self._elements(container, 0)
                     if (name := self._starter_name(element, 1)) is not None}
            if len(found) == 1:
                return found.pop()
            return UNRESOLVED_STARTER if found else None
        if (_called_name(call.func, self._imported) not in ("getattr", "builtins.getattr")
                or len(call.args) < 2):
            return None
        owner = _called_name(call.args[0], self._imported)
        for attribute in self.strings(call.args[1]):
            if attribute is UNKNOWN:
                if owner in PROCESS_MODULES:
                    return UNRESOLVED_STARTER
                continue
            if f"{owner}.{attribute}" in PROCESS_STARTERS:
                return f"{owner}.{attribute}"
            if attribute in LOOP_PROCESS_STARTERS:
                return attribute
        return None

    def _elements(self, container: ast.expr, depth: int) -> list[ast.expr]:
        """The values a dict, list, tuple or set (or a name bound to one)
        holds."""
        if isinstance(container, ast.Dict):
            return list(container.values)
        if isinstance(container, (ast.List, ast.Tuple, ast.Set)):
            return list(container.elts)
        if isinstance(container, ast.Name) and depth < 8:
            return [element for value in self._bindings.get(container.id, [])
                    for element in self._elements(value, depth + 1)]
        return []

    # --- values -------------------------------------------------------------------

    def strings(self, node: ast.expr) -> list[str | None]:
        """The possible string values of ``node`` (UNKNOWN for one the
        fence cannot resolve)."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.JoinedStr):
            known = "".join(part.value for part in node.values
                            if isinstance(part, ast.Constant))
            if any(not isinstance(part, ast.Constant) for part in node.values):
                return [known, UNKNOWN]
            return [known]
        if (isinstance(node, ast.Call) and node.args
                and _called_name(node.func, self._imported) in ("shutil.which", "os.fspath",
                                                               "str")):
            return self.strings(node.args[0])
        if (isinstance(node, ast.Attribute)
                and _called_name(node, self._imported) == "sys.executable"):
            return ["python"]
        if isinstance(node, ast.Name):
            return self._through_name(node.id, self.strings, [UNKNOWN])
        return [UNKNOWN]

    def argvs(self, node: ast.expr) -> list[list[str | None]]:
        """The possible argvs ``node`` holds (one per combination of the
        possible values of its items, at most ``MAX_ARGVS``); an argv of
        unknown length or content holds UNKNOWN."""
        if isinstance(node, (ast.List, ast.Tuple)):
            argvs: list[list[str | None]] = [[]]
            for element in node.elts:
                if isinstance(element, ast.Starred):
                    options = self.argvs(element.value)
                else:
                    options = [[value] for value in self.strings(element)]
                argvs = _combined(argvs, options)
            return argvs
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return _combined(self.argvs(node.left), self.argvs(node.right))
        if (isinstance(node, ast.Call) and len(node.args) == 1
                and _called_name(node.func, self._imported) in ("list", "tuple")):
            return self.argvs(node.args[0])
        if isinstance(node, ast.Name):
            return self._with_additions(
                node.id, self._through_name(node.id, self.argvs, [[UNKNOWN]]))
        return [[UNKNOWN]]

    def _with_additions(self, name: str,
                        argvs: list[list[str | None]]) -> list[list[str | None]]:
        """``argvs`` (what ``name`` is bound to), and each of them with every
        item the source adds to ``name`` (``extend``, ``append``, ``insert``,
        ``+=``): appended in source order, or put first where an insert may
        put it there. A program the additions change is judged as started
        (IR78-03: ``cmd = ["nice"]; cmd.extend(["git", "diff"])``)."""
        additions = self._additions.get(name)
        if not additions or name in self._resolving:
            return argvs
        self._resolving.add(name)
        try:
            added = argvs
            for first, items in additions:
                options = self.argvs(items)
                added = _combined(options, added) if first else _combined(added, options)
        finally:
            self._resolving.discard(name)
        combined = argvs + added
        if len(combined) > MAX_ARGVS:
            return combined[:MAX_ARGVS] + [[UNKNOWN]]
        return combined

    def _through_name(self, name: str, resolve, unknown):
        values = self._bindings.get(name)
        if not values or name in self._resolving:
            return unknown
        self._resolving.add(name)
        try:
            return [found for value in values for found in resolve(value)]
        finally:
            self._resolving.discard(name)

    # --- verdicts -------------------------------------------------------------------

    @staticmethod
    def _argv_verdict(argv: list[str | None]) -> str:
        if not argv or argv[0] is UNKNOWN:
            return "unproven"
        program = os.path.basename(argv[0])
        if program in GIT_PROGRAMS:
            return "git"
        if program in WRAPPER_PROGRAMS or program.startswith("python"):
            if any(value is not UNKNOWN and _names_git_word(value) for value in argv[1:]):
                return "git"
            if UNKNOWN in argv[1:]:
                return "unproven"
        return "not git"

    @staticmethod
    def _command_line_verdict(texts: list[str | None]) -> str:
        if any(text is not UNKNOWN and _names_git_word(text) for text in texts):
            return "git"
        return "unproven" if UNKNOWN in texts else "not git"

    def verdict(self, called: str, args: list[ast.expr],
                keywords: dict[str, ast.expr]) -> str:
        """The worst verdict over every value the start could be given."""
        if called == UNRESOLVED_STARTER:
            return "unproven"
        layout = dict(STARTER_ARGUMENTS.get(called, ((_ARGV, 0),)))
        candidates: list[list[str | None]] = []
        command_lines: list[str | None] = []
        shell = keywords.get("shell")
        uses_shell = shell is not None and not (
            isinstance(shell, ast.Constant) and shell.value in (False, None))
        if any(isinstance(argument, ast.Starred) for argument in args) and _REST not in layout:
            # ``run(*argv)``: where each item lands is unknown.
            return "unproven"
        if layout.get(_COMMAND_LINE, len(args)) < len(args):
            command_lines.extend(self.strings(args[layout[_COMMAND_LINE]]))
        if _REST in layout and layout[_PROGRAM] < len(args):
            # create_subprocess_exec("git", "status"): program, then arguments.
            candidates.extend(self.argvs(ast.List(elts=args[layout[_PROGRAM]:],
                                                  ctx=ast.Load())))
        elif layout.get(_ARGV, len(args)) < len(args):
            argvs = self.argvs(args[layout[_ARGV]])
            if layout.get(_PROGRAM, len(args)) < len(args):
                # os.execv(path, argv): the program is the path.
                argvs = _combined([[program] for program in
                                   self.strings(args[layout[_PROGRAM]])],
                                  [argv[1:] for argv in argvs])
            candidates.extend(argvs)
        for name, node in keywords.items():
            if name in _ARGV_KEYWORDS:
                candidates.extend(self.argvs(node))
            elif name in _PROGRAM_KEYWORDS:
                candidates.extend([value] for value in self.strings(node))
        if uses_shell:
            command_lines.extend(argv[0] for argv in candidates if argv)
            command_lines.extend(value for argv in candidates for value in argv[1:])
            candidates = []
        verdicts = [self._argv_verdict(argv) for argv in candidates]
        if command_lines:
            verdicts.append(self._command_line_verdict(command_lines))
        if not verdicts:
            return "unproven"
        for worst in ("git", "unproven"):
            if worst in verdicts:
                return worst
        return "not git"


def _process_starts(source: str, filename: str) -> list[ProcessStart]:
    """Every call in ``source`` that starts a process, with the function it
    is in (``<module>`` at module level) and the fence's verdict on it."""
    tree = ast.parse(source, filename=filename)
    imported = _imported_names(tree)
    fence = _ProcessStartFence(tree, imported)
    starts: list[ProcessStart] = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = function
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = child.name
            if isinstance(child, ast.Call):
                started = fence.starter(child)
                if started is not None:
                    called, args, keywords = started
                    starts.append(ProcessStart(child.lineno, inner, called,
                                               fence.verdict(called, args, keywords)))
            visit(child, inner)

    visit(tree, "<module>")
    return starts


def _git_started_outside_the_runners(
    source: str, filename: str, *, allowed_functions: frozenset[str] = frozenset(),
    unproven_allowed: frozenset[str] = frozenset(),
) -> list[str]:
    """``filename:line`` of each call in ``source`` that starts git or gh,
    or a program the fence cannot prove is not git or gh (fail closed),
    outside ``allowed_functions`` (the runners). A start in one of
    ``unproven_allowed`` (functions reviewed in ``UNPROVEN_STARTS``) is
    accepted when the fence cannot prove its program, never when it proves
    it is git."""
    return [
        start.describe(filename)
        for start in _process_starts(source, filename)
        if start.function not in allowed_functions
        and start.verdict != "not git"
        and not (start.verdict == "unproven" and start.function in unproven_allowed)
    ]


def _orchestrator_sources() -> list[Path]:
    """Every Python file of the orchestrator: the equipa package and its
    entry point."""
    sources = sorted((REPO_ROOT / "equipa").rglob("*.py"))
    entry_point = REPO_ROOT / "forge_orchestrator.py"
    if entry_point.is_file():
        sources.append(entry_point)
    return sources


# Inside git_ops.py only these two functions may start a process: they are
# the runners the non-git guard lives in (IR76-02: the module itself is no
# longer exempt, so a new git_ops helper that starts git is found too).
RUNNER_FUNCTIONS = frozenset({"_run_with_env", "_run_git_process_async"})

# Process starts whose program the fence cannot resolve, each read and
# found not to start the orchestrator's git or gh in a project. Keyed by
# (module, function): a new start in one of these functions whose program
# the fence PROVES is git is still found; one in any other function needs
# an entry (and a reason) here.
UNPROVEN_STARTS: dict[tuple[str, str], str] = {
    ("equipa/agent_launcher.py", "_git"):
        "the launcher is a separate program (python -I, imports nothing from "
        "equipa) that git-inits its own session repository under the unit's "
        "state directory, with GIT_CONFIG_GLOBAL=/dev/null; the orchestrator's "
        "non-git record cannot reach that process",
    ("equipa/agent_launcher.py", "_verify_no_scheduler"):
        "crontab -l / at -l from the fixed _SCHEDULERS paths",
    ("equipa/agent_launcher.py", "_run_isolated"):
        "starts the contained agent CLI, the program the launcher exists for",
    ("equipa/agent_launcher.py", "main"):
        "execs or spawns the contained agent CLI given by --executable",
    ("equipa/agent_runner.py", "_gate_canary_ok"):
        "runs the configured PreToolUse hook command with a canary payload",
    ("equipa/agent_runner.py", "_spawn_unisolated"):
        "starts the agent CLI command, or the launcher (sys.executable -I)",
    ("equipa/generated_files.py", "run_generator"):
        "sys.executable -I -B <generator script> in the export root (GeneratedFile.argv)",
    ("equipa/hooks/__init__.py", "run_external_hook"):
        "an operator-configured external hook command line",
    ("equipa/hooks/__init__.py", "run_external_hook_async"):
        "an operator-configured external hook command line",
    ("equipa/isolation.py", "_spawn_in_slot"):
        "build_launch_command: the isolation unit launcher for the agent",
    ("equipa/isolation.py", "_exit_status"):
        "sudo -n checks of the agent user (sudo -l, nft list table)",
    ("equipa/isolation.py", "_run_capture"):
        "sudo -n -l -U <agent user>",
    ("equipa/mcp_server.py", "_handle_equipa_dispatch"):
        "starts a detached orchestrator run (sys.executable -m ...)",
    ("equipa/preflight.py", "_run_install_cmd"):
        "a detected dependency install command, refused in a task worktree",
    ("equipa/preflight.py", "preflight_build_check"):
        "a detected build command, refused in a task worktree",
    ("equipa/reactive_check.py", "_start_worker"):
        "the reactive-check worker (sys.executable, the orchestrator's own code)",
}

HELPERS_THAT_START_GIT_THEMSELVES = {
    "subprocess.run-list": 'import subprocess\nsubprocess.run(["git", "diff"], cwd=p)\n',
    "subprocess.Popen-tuple": 'import subprocess\nsubprocess.Popen(("gh", "pr", "list"))\n',
    "check_output-absolute-path":
        'import subprocess\nsubprocess.check_output(["/usr/bin/git", "status"])\n',
    "from-import-alias": 'from subprocess import run as start\nstart(["git", "log"])\n',
    "module-alias": 'import subprocess as sp\nsp.call(["git", "fetch"])\n',
    "argv-built-first":
        'import subprocess\nargv = ["git", "diff", "--stat"]\nsubprocess.run(argv, cwd=p)\n',
    "argv-through-two-names":
        'import subprocess\ncommand = base\nbase = ["git"]\nsubprocess.run([*command, "gc"])\n',
    "program-found-with-which":
        'import shutil, subprocess\nGIT = shutil.which("git")\nsubprocess.run([GIT, "status"])\n',
    "create_subprocess_exec":
        'import asyncio\nawait asyncio.create_subprocess_exec("git", "status")\n',
    "loop.subprocess_exec": 'await loop.subprocess_exec(factory, "git", "status")\n',
    "os.system-command-line": 'import os\nos.system("git status --short")\n',
    "shell-command-line":
        'import subprocess\nsubprocess.run("gh pr list", shell=True, cwd=p)\n',
    "os.execvp": 'import os\nos.execvp("git", ["git", "gc"])\n',
    "windows-program": 'import subprocess\nsubprocess.run(["git.exe", "diff"])\n',
    # IR76-02 (task #3178): the shapes the first fence did not see.
    "argv-concatenated": 'import subprocess\nsubprocess.run(["git"] + args, cwd=p)\n',
    "argv-concatenated-first":
        'import subprocess\nargv = ["git"] + list(args)\nsubprocess.run(argv)\n',
    "args-keyword": 'import subprocess\nsubprocess.run(args=["git", "status"])\n',
    "list-call": 'import subprocess\nsubprocess.run(list(("git", "status")))\n',
    "tuple-call-of-a-name":
        'import subprocess\nBASE = ("gh", "pr")\nsubprocess.run(tuple(BASE))\n',
    "functools.partial":
        'import functools, subprocess\n'
        'start = functools.partial(subprocess.run, ["git", "diff"])\nstart()\n',
    "partial-imported":
        'from functools import partial\nimport subprocess\n'
        'partial(subprocess.Popen, ["gh", "api"])()\n',
    "starter-bound-to-a-name":
        'import subprocess\nrunner = subprocess.run\nrunner(["git", "log"])\n',
    "attribute-program":
        'import subprocess\nsubprocess.run([settings.git_executable, "status"])\n',
    "call-program": 'import subprocess\nsubprocess.run([_git_binary(), "status"])\n',
    "parameter-argv": 'import subprocess\ndef helper(argv):\n    subprocess.run(argv)\n',
    "executable-keyword":
        'import subprocess\nsubprocess.run(["x", "status"], executable="/usr/bin/git")\n',
    "env-wrapper": 'import subprocess\nsubprocess.run(["env", "git", "-C", p, "diff"])\n',
    "env-assignment-wrapper":
        'import subprocess\nsubprocess.run(["/usr/bin/env", "GIT_DIR=x", "gh", "pr"])\n',
    "sh-c-wrapper":
        'import subprocess\nsubprocess.run(["sh", "-c", "cd project && git diff"])\n',
    "sh-c-f-string-wrapper":
        'import subprocess\nsubprocess.run(["bash", "-c", f"cd {p}; git status"])\n',
    "shell-wrapper-of-unknown-command":
        'import subprocess\nsubprocess.run(["sh", "-c", command])\n',
    "timeout-wrapper": 'import subprocess\nsubprocess.run(["timeout", "30", "git", "gc"])\n',
    "python-wrapper":
        'import subprocess, sys\n'
        'subprocess.run([sys.executable, "-c", "import os; os.system(\'git gc\')"])\n',
    "command-line-git-later": 'import os\nos.system("cd project && git diff")\n',
    "shell-true-list": 'import subprocess\nsubprocess.run(["cd p; git diff"], shell=True)\n',
    "create_subprocess_exec-wrapper":
        'import asyncio\nawait asyncio.create_subprocess_exec("env", "git", "status")\n',
    "posix_spawn": 'import os\nos.posix_spawn("/usr/bin/git", ["git", "gc"], env)\n',
    "spawnv": 'import os\nos.spawnv(os.P_WAIT, "/usr/bin/git", ["git", "gc"])\n',
    # IR78-03 (task #3180): the shapes the IR76-02 fence did not see.
    "argv-extended":
        'import subprocess\ncmd = ["nice"]\ncmd.extend(["git", "diff"])\nsubprocess.run(cmd)\n',
    "argv-appended": 'import subprocess\ncmd = ["env"]\ncmd.append("gh")\nsubprocess.run(cmd)\n',
    "argv-inserted-first":
        'import subprocess\ncmd = ["status"]\ncmd.insert(0, "git")\nsubprocess.run(cmd)\n',
    "argv-inserted-anywhere":
        'import subprocess\ncmd = ["status"]\ncmd.insert(i, "git")\nsubprocess.run(cmd)\n',
    "argv-added-after-an-assignment-word":
        'import subprocess\ncmd = ["env"]\ncmd += ["LANG=C", "git", "diff"]\nsubprocess.run(cmd)\n',
    "getattr-starter": 'import subprocess\ngetattr(subprocess, "run")(["git", "diff"])\n',
    "getattr-starter-bound-to-a-name":
        'import subprocess\nstart = getattr(subprocess, "Popen")\nstart(["git", "log"])\n',
    "getattr-of-an-unknown-name": 'import subprocess\ngetattr(subprocess, name)(["x"])\n',
    "pty.spawn": 'import pty\npty.spawn(["git", "log"])\n',
    "dict-of-starters":
        'import subprocess\nSTARTERS = {"run": subprocess.run}\nSTARTERS["run"](["git", "diff"])\n',
    "dict-of-starters-get":
        'import subprocess\nSTARTERS = {"run": subprocess.run}\n'
        'STARTERS.get("run")(["git", "status"])\n',
    "list-of-starters": 'import os, subprocess\n[subprocess.run, os.system][i](argv)\n',
}


@pytest.mark.parametrize("shape", sorted(HELPERS_THAT_START_GIT_THEMSELVES))
def test_the_fence_finds_a_helper_that_starts_git_itself(shape: str) -> None:
    """Positive control: each way a new helper could start git without the
    runners is found."""
    source = HELPERS_THAT_START_GIT_THEMSELVES[shape]

    assert _git_started_outside_the_runners(source, f"{shape}.py") != []


CODE_THAT_DOES_NOT_START_GIT = {
    "the-runners": 'from equipa.git_ops import git_run\ngit_run(["diff", "--stat"], p)\n',
    "another-program": 'import subprocess\nsubprocess.run(["python3", "-c", "pass"])\n',
    "comment": '# subprocess.run(["git", "diff"])\nx = 1\n',
    "docstring": 'def f():\n    """subprocess.run(["git", "diff"]) is not used."""\n',
    "message-mentioning-git": 'log(["git status failed", detail])\n',
    "a-list-of-command-names": 'SAFE = frozenset(["git", "go", "date"])\n',
    "an-argv-that-is-not-started": 'DIFF = ("git", "diff")\nprint(" ".join(DIFF))\n',
    "git-as-an-argument": 'import subprocess\nsubprocess.run(["grep", "git", path])\n',
    "a-word-starting-with-git":
        'import subprocess\nsubprocess.run(["gitleaks", "detect"])\n',
    "a-wrapper-of-another-program":
        'import subprocess\nsubprocess.run(["env", "LANG=C", "nft", "list", "ruleset"])\n',
    "a-known-argv-built-by-concatenation":
        'import subprocess\nBASE = ["crontab"]\nsubprocess.run(BASE + ["-l"])\n',
    "a-python-wrapper-of-known-code":
        'import subprocess, sys\nsubprocess.run([sys.executable, "-c", "pass"])\n',
    "os.spawnv-of-another-program":
        'import os\nos.spawnv(os.P_WAIT, "/usr/bin/nft", ["nft", "list"])\n',
    "an-argv-of-another-program-extended":
        'import subprocess\ncmd = ["nft"]\ncmd.extend(["list", "ruleset"])\n'
        'cmd.append("-j")\nsubprocess.run(cmd)\n',
    "a-dict-of-other-callables": 'HANDLERS = {"a": print}\nHANDLERS["a"]("git")\n',
    "getattr-of-another-object": 'getattr(logger, "info")(["git", "diff"])\n',
    "getattr-of-another-starter-name":
        'import subprocess\ngetattr(subprocess, "DEVNULL")\n',
}


@pytest.mark.parametrize("shape", sorted(CODE_THAT_DOES_NOT_START_GIT))
def test_the_fence_passes_code_that_does_not_start_git(shape: str) -> None:
    """Negative control: mentions of git that start nothing are not found."""
    source = CODE_THAT_DOES_NOT_START_GIT[shape]

    assert _git_started_outside_the_runners(source, f"{shape}.py") == []


def test_the_fence_finds_git_started_in_a_real_module() -> None:
    """Control on real code: monitoring.py with each of its ``git_run([...])``
    change checks rewritten to start git itself is found once per call, so
    the scan below is not vacuous on the shape of the orchestrator's code."""
    module = REPO_ROOT / "equipa" / "monitoring.py"
    source = module.read_text(encoding="utf-8")
    calls = source.count("git_run([")
    started_itself = source.replace("git_run([", 'subprocess.run(["git", ')

    assert calls > 0
    assert _git_started_outside_the_runners(source, module.name) == []
    assert len(_git_started_outside_the_runners(started_itself, module.name)) == calls


PLANTED_FORGETFUL_HELPERS = {
    "extended-argv": (
        "def _planted_forgetful_status(project):\n"
        "    cmd = [\"nice\"]\n"
        "    cmd.extend([\"git\", \"status\"])\n"
        "    return subprocess.run(cmd, cwd=project)\n"),
    "getattr-starter": (
        "def _planted_forgetful_status(project):\n"
        "    return getattr(subprocess, \"run\")([\"git\", \"status\"], cwd=project)\n"),
    "pty-spawn": (
        "import pty\n\n\n"
        "def _planted_forgetful_status(project):\n"
        "    return pty.spawn([\"git\", \"-C\", project, \"status\"])\n"),
    "dict-of-starters": (
        "_PLANTED_STARTERS = {\"run\": subprocess.run}\n\n\n"
        "def _planted_forgetful_status(project):\n"
        "    return _PLANTED_STARTERS[\"run\"]([\"git\", \"status\"], cwd=project)\n"),
}


@pytest.mark.parametrize("shape", sorted(PLANTED_FORGETFUL_HELPERS))
def test_the_fence_finds_an_ir78_03_helper_planted_in_a_real_module(shape: str) -> None:
    """IR78-03: the reviewer planted the ``extend`` shape in a scratch copy
    of monitoring.py and the scan passed; each shape is found there now."""
    module = REPO_ROOT / "equipa" / "monitoring.py"
    source = module.read_text(encoding="utf-8")
    planted = source + "\n\n" + PLANTED_FORGETFUL_HELPERS[shape]

    assert _git_started_outside_the_runners(source, module.name) == []
    found = _git_started_outside_the_runners(planted, module.name)
    assert len(found) == 1 and "_planted_forgetful_status" in found[0], found


def test_no_orchestrator_module_starts_git_outside_the_runners() -> None:
    """IR73-02: every git and gh process the orchestrator starts goes
    through the git_ops runners, which refuse a project that was not git, so
    a helper that forgets to ask cannot run git there by starting it
    itself either."""
    sources = _orchestrator_sources()
    found = []
    for source in sources:
        name = source.relative_to(REPO_ROOT).as_posix()
        found.extend(_git_started_outside_the_runners(
            source.read_text(encoding="utf-8"), name,
            allowed_functions=RUNNER_FUNCTIONS if source == RUNNERS_MODULE else frozenset(),
            unproven_allowed=frozenset(
                function for module, function in UNPROVEN_STARTS if module == name),
        ))

    assert RUNNERS_MODULE in sources
    assert len(sources) > 50, [str(source) for source in sources]
    assert found == []


def test_every_reviewed_unproven_start_is_still_one() -> None:
    """IR76-02: an UNPROVEN_STARTS entry names a function that still starts
    a program the fence cannot prove (no stale entry that would let a new
    one through), and none of them is proven to start git; the runners'
    own starts are the only ones git_ops.py keeps."""
    unproven: set[tuple[str, str]] = set()
    proven_git: list[str] = []
    for source in _orchestrator_sources():
        name = source.relative_to(REPO_ROOT).as_posix()
        for start in _process_starts(source.read_text(encoding="utf-8"), name):
            if start.verdict == "unproven":
                unproven.add((name, start.function))
            elif start.verdict == "git":
                proven_git.append(start.describe(name))
    runner_starts = {("equipa/git_ops.py", function) for function in RUNNER_FUNCTIONS}

    assert set(UNPROVEN_STARTS) == unproven - runner_starts
    assert runner_starts <= unproven
    assert proven_git == []
    assert all(reason.strip() for reason in UNPROVEN_STARTS.values())


def test_a_new_git_ops_helper_that_starts_git_is_found() -> None:
    """IR76-02: git_ops.py was exempt as a whole module; now only the two
    runner functions may start a process there."""
    source = RUNNERS_MODULE.read_text(encoding="utf-8") + (
        "\n\ndef _forgetful_helper(cwd):\n"
        "    return subprocess.run([\"git\", \"diff\"], cwd=cwd)\n"
    )
    found = _git_started_outside_the_runners(
        source, "equipa/git_ops.py", allowed_functions=RUNNER_FUNCTIONS)

    assert len(found) == 1 and "_forgetful_helper" in found[0], found


def test_every_starter_has_an_argument_layout() -> None:
    assert set(STARTER_ARGUMENTS) >= PROCESS_STARTERS | LOOP_PROCESS_STARTERS
