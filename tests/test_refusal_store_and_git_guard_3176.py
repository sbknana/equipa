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
import shutil
import subprocess
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


FORGETFUL_HELPERS = {
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
})
# Event-loop methods, called on a loop object whose name the source does
# not fix.
LOOP_PROCESS_STARTERS = frozenset({"subprocess_exec", "subprocess_shell"})


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


class _GitNaming:
    """Decides whether an expression names git or gh as the program to
    start, following the names a module binds to one."""

    def __init__(self, tree: ast.Module, imported: dict[str, str]) -> None:
        self._imported = imported
        self._names: set[str] = set()
        # A name bound to git, to a git argv, or to another such name: bind
        # until nothing changes, so the order of the assignments is free.
        assignments = [
            (target.id, node.value)
            for node in ast.walk(tree) if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
        ]
        changed = True
        while changed:
            changed = False
            for name, value in assignments:
                if name not in self._names and (
                    self.names_git(value, command_line=False) or self._finds_git(value)
                ):
                    self._names.add(name)
                    changed = True

    def _finds_git(self, node: ast.expr) -> bool:
        """``shutil.which("git")`` and the like."""
        return (
            isinstance(node, ast.Call)
            and _called_name(node.func, self._imported) == "shutil.which"
            and any(self.names_git(argument, command_line=False) for argument in node.args)
        )

    def names_git(self, node: ast.expr, *, command_line: bool) -> bool:
        """True when ``node`` names git or gh: a string that is the program
        (``git``, ``/usr/bin/git``) or, with ``command_line``, starts with
        it (``git status``); a list or tuple whose first item names it (an
        argv); or a name bound to one of those."""
        if isinstance(node, ast.Starred):
            return self.names_git(node.value, command_line=command_line)
        if isinstance(node, ast.Name):
            return node.id in self._names
        if isinstance(node, (ast.List, ast.Tuple)):
            return bool(node.elts) and self.names_git(node.elts[0], command_line=False)
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            return False
        words = node.value.split() if command_line else [node.value]
        return bool(words) and os.path.basename(words[0]) in GIT_PROGRAMS


def _git_started_outside_the_runners(source: str, filename: str) -> list[str]:
    """``filename:line`` of each call in ``source`` that starts a process
    and is given git or gh as the program: in an argv, as the program
    argument, or at the start of a command line."""
    tree = ast.parse(source, filename=filename)
    imported = _imported_names(tree)
    naming = _GitNaming(tree, imported)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = _called_name(node.func, imported)
        starts_a_process = called in PROCESS_STARTERS or (
            called is not None and called.rsplit(".", 1)[-1] in LOOP_PROCESS_STARTERS
        )
        if starts_a_process and any(
            naming.names_git(argument, command_line=True) for argument in node.args
        ):
            found.append(f"{filename}:{node.lineno}: {called} starts git")
    return found


def _orchestrator_sources() -> list[Path]:
    """Every Python file of the orchestrator: the equipa package and its
    entry point."""
    sources = sorted((REPO_ROOT / "equipa").rglob("*.py"))
    entry_point = REPO_ROOT / "forge_orchestrator.py"
    if entry_point.is_file():
        sources.append(entry_point)
    return sources


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


def test_no_orchestrator_module_starts_git_outside_the_runners() -> None:
    """IR73-02: every git and gh process the orchestrator starts goes
    through the git_ops runners, which refuse a project that was not git, so
    a helper that forgets to ask cannot run git there by starting it
    itself either."""
    sources = _orchestrator_sources()
    found = [
        place
        for source in sources
        if source != RUNNERS_MODULE
        for place in _git_started_outside_the_runners(
            source.read_text(encoding="utf-8"), str(source.relative_to(REPO_ROOT)),
        )
    ]

    assert RUNNERS_MODULE in sources
    assert len(sources) > 50, [str(source) for source in sources]
    assert found == []
