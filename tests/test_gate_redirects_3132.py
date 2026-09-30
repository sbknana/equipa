"""Task #3132: fix-forward of task #3126 after its independent review.

Each test fails on the pre-fix main on its assertion (or on a missing name
the fix introduces); tests named ``..._control`` pin behaviour that must not
change.

* IND-02 (MEDIUM, regression of 3126): an agent-written ``core.worktree``
  (repo config, worktree config or an included file) made
  ``rev-parse --show-toplevel`` name ANOTHER repository. The gate re-rooted
  there, scanned that repository for hazards, diffed its same-named branch
  and merged into ITS default branch, while this project's task was
  recorded merged. Now the hazard scan runs on the operator's paths first,
  a work-tree root must contain the directory it is the root of, a
  ``.git`` file planted in a nested project is caught by the common-dir
  check, and ``core.bare`` is refused.
* IND-01 (MEDIUM): ``diff.ignoreSubmodules``, ``submodule.<name>.ignore``
  or ``ignore = all`` committed in ``.gitmodules`` hid a submodule pointer
  bump, so a gitlink change plus a README read as doc-only and merged
  unreviewed. The gate diff passes ``--ignore-submodules=none``, the
  hardened helper pins ``diff.ignoreSubmodules=none``, a gitlink is never
  doc-only and the hazard scan refuses both config keys.
* IND-03 (LOW): with no git binary a directory without any ``.git`` is not
  git (it runs as before); one with a ``.git`` is refused.
* R3126-03 / IND-04 (INFO): the goal planner and evaluator get no MCP server,
  so TheForge's ``write_query`` is out of reach; the manager creates the tasks
  they return in a ``TASKS_JSON`` block.
* IR-04 (MEDIUM, from the 3127 review): git and gh children get an
  allowlisted environment; GitHub credentials only reach gh and git push.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import equipa.dispatch as dispatch_mod
import equipa.git_ops as git_ops_mod
import equipa.manager as manager_mod
from equipa.constants import MCP_CONFIG
from equipa.merge_integrity import find_repo_execution_hazards, index_flag_problem
from equipa.security_gate import get_changed_files_for_branch, is_doc_only_diff

from test_dispatch_modes_gated_3112 import _commit_files, _git, _init_repo, _master

REPO_ROOT = Path(__file__).resolve().parent.parent
TASK_ID = 3132
TASK_BRANCH = f"forge-task-{TASK_ID}"
PAYLOAD = "import os\nos.system('echo FAKE-SENTINEL-3132')\n"
# Two made-up submodule commits: the gitlink only records the SHA.
SUBMODULE_BASE = "1" * 40
SUBMODULE_BUMP = "2" * 40
SECRET_SENTINELS = {
    "DATABASE_URL": "postgresql://fake-user:fake-pass-3132@db.invalid/fake",
    "PGPASSWORD": "fake-pgpassword-3132",
    "GH_TOKEN": "ghp_FAKE3132TOKENxxxxxxxxxxxxxxxxxxxxxx",
    "GITHUB_TOKEN": "github_pat_FAKE3132xxxxxxxxxxxxxxxx",
    "ANTHROPIC_API_KEY": "sk-ant-FAKE-3132",
    "EQUIPA_FAKE_SECRET_3132": "fake-secret-value-3132",
}


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# IND-02: core.worktree / GIT_DIR-style redirects
# ---------------------------------------------------------------------------

def _redirect_pair(tmp_path: Path) -> tuple[Path, Path]:
    """``real`` (task branch adds code) and ``other`` (same branch, README only).

    This is the independent reviewer's probe: a gate that follows
    ``core.worktree`` from ``real`` to ``other`` sees a doc-only diff and
    merges it into ``other``'s default branch.
    """
    real = _init_repo(tmp_path / "real")
    _git(real, "checkout", "-q", "-b", TASK_BRANCH)
    _commit_files(real, {"lib/code.py": PAYLOAD}, "task code")
    _git(real, "checkout", "-q", "master")

    other = _init_repo(tmp_path / "other")
    _git(other, "checkout", "-q", "-b", TASK_BRANCH)
    _commit_files(other, {"README.md": "decoy docs\n"}, "decoy doc change")
    _git(other, "checkout", "-q", "master")
    return real, other


def _set_core_worktree(real: Path, other: Path, scope: str, tmp_path: Path) -> None:
    if scope == "local":
        _git(real, "config", "core.worktree", str(other))
    elif scope == "worktree":
        _git(real, "config", "extensions.worktreeConfig", "true")
        _git(real, "config", "--worktree", "core.worktree", str(other))
    elif scope == "include":
        included = tmp_path / "included.gitconfig"
        included.write_text(f"[core]\n\tworktree = {other}\n", encoding="utf-8")
        _git(real, "config", "include.path", str(included))
    else:  # pragma: no cover - parametrisation guard
        raise AssertionError(scope)


@pytest.mark.parametrize("scope", ["local", "worktree", "include"])
def test_core_worktree_does_not_move_the_gate_or_the_merge_to_another_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture, scope: str,
) -> None:
    real, other = _redirect_pair(tmp_path)
    real_master, other_master = _master(real), _master(other)
    _set_core_worktree(real, other, scope, tmp_path)

    status = _run(dispatch_mod._gated_merge_task(
        repo=str(real), branch=TASK_BRANCH, outcome="tests_passed",
        task_id=TASK_ID,
    ))

    assert status == "blocked"
    assert _master(other) == other_master, "the other repository's master moved"
    assert _master(real) == real_master, "unreviewed code reached the project"
    assert "core.worktree" in capsys.readouterr().out


def test_containment_blocks_a_redirected_root_even_without_the_hazard_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: the root check refuses on its own."""
    real, other = _redirect_pair(tmp_path)
    other_master = _master(other)
    _git(real, "config", "core.worktree", str(other))

    async def no_hazards(_directory):
        return []

    monkeypatch.setattr(dispatch_mod, "find_repo_execution_hazards", no_hazards)
    status = _run(dispatch_mod._gated_merge_task(
        repo=str(real), branch=TASK_BRANCH, outcome="tests_passed",
        task_id=TASK_ID,
    ))

    assert status == "blocked"
    assert _master(other) == other_master


def test_work_tree_root_must_contain_the_directory(tmp_path: Path) -> None:
    real, other = _redirect_pair(tmp_path)
    _git(real, "config", "core.worktree", str(other))

    assert git_ops_mod.git_toplevel(real) is None
    assert _run(git_ops_mod.git_toplevel_async(real)) is None
    with pytest.raises(git_ops_mod.GitRepositoryUnreadableError):
        git_ops_mod._is_git_repo(real)


def test_gate_diff_and_index_check_refuse_a_redirected_root(tmp_path: Path) -> None:
    real, other = _redirect_pair(tmp_path)
    _git(real, "config", "core.worktree", str(other))

    changed = _run(get_changed_files_for_branch(
        str(real), base_ref="master", head_ref=TASK_BRANCH,
    ))

    assert changed == [], f"the gate diffed another repository: {changed}"
    assert not is_doc_only_diff(changed)
    assert _run(index_flag_problem(real)) is not None


def test_retry_cleanup_refuses_a_redirected_repository(tmp_path: Path) -> None:
    real, other = _redirect_pair(tmp_path)
    _git(real, "config", "core.worktree", str(other))
    other_branch = _git(other, "rev-parse", f"refs/heads/{TASK_BRANCH}")

    with pytest.raises(dispatch_mod.AttemptCleanupError):
        _run(dispatch_mod.cleanup_failed_attempt(TASK_ID, str(real), []))

    assert _git(other, "rev-parse", f"refs/heads/{TASK_BRANCH}") == other_branch


def test_work_tree_root_of_a_plain_repository_control(tmp_path: Path) -> None:
    real, _other = _redirect_pair(tmp_path)
    (real / "sub").mkdir()

    assert git_ops_mod.git_toplevel(real / "sub") == real.resolve()
    assert git_ops_mod._is_git_repo(real / "sub") is True


def test_planted_git_file_in_a_nested_project_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """A ``.git`` file (GIT_DIR-style redirect) inside the nested project."""
    real, other = _redirect_pair(tmp_path)
    _commit_files(real, {"sub/app.py": "print('sub')\n"}, "nested project")
    _git(real, "branch", "-f", TASK_BRANCH, "master")
    _git(real, "checkout", "-q", TASK_BRANCH)
    _commit_files(real, {"lib/code.py": PAYLOAD}, "task code")
    _git(real, "checkout", "-q", "master")
    worktree = tmp_path / "task-worktree"
    _git(real, "worktree", "add", "-q", str(worktree), TASK_BRANCH)
    (real / "sub" / ".git").write_text(f"gitdir: {other / '.git'}\n", encoding="utf-8")
    other_master = _master(other)
    real_master = _master(real)

    status = _run(dispatch_mod._gated_merge_task(
        repo=str(real / "sub"), branch=TASK_BRANCH, outcome="tests_passed",
        task_id=TASK_ID, worktree_dir=str(worktree / "sub"),
    ))

    assert status == "blocked"
    assert _master(other) == other_master, "the other repository's master moved"
    assert _master(real) == real_master
    assert "GIT_DIR" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("value", "flagged"), [("true", True), ("false", False)],
    ids=["bare", "not-bare-control"],
)
def test_hazard_scan_refuses_core_bare(
    tmp_path: Path, value: str, flagged: bool,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "--worktree", "core.bare", value)

    hazards = _run(find_repo_execution_hazards(repo))

    assert any("core.bare" in hazard for hazard in hazards) is flagged, hazards


def test_operator_git_dir_env_does_not_redirect_orchestrator_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real, other = _redirect_pair(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))

    assert git_ops_mod.git_toplevel(real) == real.resolve()


# ---------------------------------------------------------------------------
# IND-01: submodule pointer bumps hidden from the gate
# ---------------------------------------------------------------------------

def _set_gitlink(repo: Path, path: str, sha: str) -> None:
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},{path}")


def _submodule_repo(tmp_path: Path, route: str, link_path: str = "vendor/lib") -> Path:
    """Repo whose task branch bumps a gitlink and edits the README."""
    repo = _init_repo(tmp_path / "super")
    gitmodules = (
        f'[submodule "{link_path}"]\n\tpath = {link_path}\n'
        f"\turl = https://example.invalid/lib.git\n"
    )
    if route == "gitmodules":
        gitmodules += "\tignore = all\n"
    (repo / ".gitmodules").write_text(gitmodules, encoding="utf-8")
    _git(repo, "add", ".gitmodules")
    _set_gitlink(repo, link_path, SUBMODULE_BASE)
    _git(repo, "commit", "-q", "-m", "add submodule")
    _git(repo, "checkout", "-q", "-b", TASK_BRANCH)
    _set_gitlink(repo, link_path, SUBMODULE_BUMP)
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "bump submodule plus docs")
    _git(repo, "checkout", "-q", "master")
    if route == "diff-config":
        _git(repo, "config", "diff.ignoreSubmodules", "all")
    elif route == "submodule-config":
        _git(repo, "config", f"submodule.{link_path}.ignore", "all")
    return repo


SUBMODULE_ROUTES = ["diff-config", "submodule-config", "gitmodules"]


@pytest.mark.parametrize("route", SUBMODULE_ROUTES)
def test_gate_diff_lists_a_hidden_submodule_bump(tmp_path: Path, route: str) -> None:
    repo = _submodule_repo(tmp_path, route)

    changed = _run(get_changed_files_for_branch(
        str(repo), base_ref="master", head_ref=TASK_BRANCH,
    ))

    assert sorted(changed) == ["README.md", "vendor/lib"], changed
    assert not is_doc_only_diff(changed)


@pytest.mark.parametrize("route", SUBMODULE_ROUTES)
def test_hidden_submodule_bump_is_not_merged_unreviewed(
    tmp_path: Path, route: str,
) -> None:
    repo = _submodule_repo(tmp_path, route)
    baseline = _master(repo)

    status = _run(dispatch_mod._gated_merge_task(
        repo=str(repo), branch=TASK_BRANCH, outcome="tests_passed",
        task_id=TASK_ID,
    ))

    assert status == "blocked"
    assert _master(repo) == baseline
    assert _git(repo, "rev-parse", "master:vendor/lib") == SUBMODULE_BASE


def test_submodule_pointer_with_a_doc_name_is_never_doc_only(tmp_path: Path) -> None:
    repo = _submodule_repo(tmp_path, "none", link_path="NOTES.md")

    changed = _run(get_changed_files_for_branch(
        str(repo), base_ref="master", head_ref=TASK_BRANCH,
    ))

    assert "NOTES.md" in changed
    assert not is_doc_only_diff(changed)


def test_plain_doc_change_is_still_doc_only_control(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "checkout", "-q", "-b", TASK_BRANCH)
    _commit_files(repo, {"README.md": "better docs\n"}, "docs")
    _git(repo, "checkout", "-q", "master")

    changed = _run(get_changed_files_for_branch(
        str(repo), base_ref="master", head_ref=TASK_BRANCH,
    ))

    assert changed == ["README.md"]
    assert is_doc_only_diff(changed)


def test_hardened_git_pins_diff_ignore_submodules(tmp_path: Path) -> None:
    repo = _submodule_repo(tmp_path, "diff-config")

    result = git_ops_mod.git_run(
        ["diff", "--name-only", f"master...{TASK_BRANCH}"], repo,
    )

    assert result.returncode == 0, result.stderr
    assert "vendor/lib" in result.stdout.split()


@pytest.mark.parametrize(
    ("key", "value", "flagged"),
    [
        ("diff.ignoreSubmodules", "all", True),
        ("diff.ignoreSubmodules", "dirty", True),
        ("submodule.vendor/lib.ignore", "all", True),
        ("diff.ignoreSubmodules", "none", False),
        ("submodule.vendor/lib.ignore", "none", False),
    ],
    ids=["diff-all", "diff-dirty", "submodule-all", "diff-none-control",
         "submodule-none-control"],
)
def test_hazard_scan_refuses_submodule_ignore_config(
    tmp_path: Path, key: str, value: str, flagged: bool,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "config", key, value)

    hazards = _run(find_repo_execution_hazards(repo))

    assert any(key.lower() in hazard.lower() for hazard in hazards) is flagged, hazards


# ---------------------------------------------------------------------------
# IND-03: no git binary
# ---------------------------------------------------------------------------

def _without_git_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def test_plain_directory_without_git_binary_is_not_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    _without_git_on_path(tmp_path, monkeypatch)

    assert git_ops_mod._is_git_repo(plain) is False
    assert git_ops_mod.git_toplevel(plain) is None


def test_repository_without_git_binary_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "nested").mkdir()
    _without_git_on_path(tmp_path, monkeypatch)

    for directory in (repo, repo / "nested"):
        with pytest.raises(git_ops_mod.GitRepositoryUnreadableError):
            git_ops_mod._is_git_repo(directory)


# ---------------------------------------------------------------------------
# R3126-03 / IND-04: goal agents reach no DB write tool
# ---------------------------------------------------------------------------

def _goal_agent_command(
    monkeypatch: pytest.MonkeyPatch, role: str, tmp_path: Path,
) -> list[str]:
    """The real command ``build_cli_command`` produces, as run_agent sees it."""
    captured: list[list[str]] = []

    async def fake_run_agent(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return {"success": False, "errors": ["stubbed"], "duration": 0.0}

    monkeypatch.setattr(manager_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(manager_mod, "build_planner_prompt", lambda *a, **k: "plan")
    monkeypatch.setattr(manager_mod, "build_evaluator_prompt", lambda *a, **k: "judge")
    args = SimpleNamespace(model="m", max_turns=10, dispatch_config={})
    if role == "planner":
        _run(manager_mod.run_planner_agent("goal", 9001, str(tmp_path), {}, args))
    else:
        _run(manager_mod.run_evaluator_agent(
            "goal", 9001, str(tmp_path), {}, [], [], args,
        ))
    assert len(captured) == 1
    return captured[0]


@pytest.mark.parametrize("role", ["planner", "evaluator"])
def test_goal_agents_get_no_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str,
) -> None:
    cmd = _goal_agent_command(monkeypatch, role, tmp_path)

    assert str(MCP_CONFIG) not in cmd, "TheForge MCP config reached a goal agent"
    assert "--strict-mcp-config" in cmd
    configs = [cmd[i + 1] for i, token in enumerate(cmd) if token == "--mcp-config"]
    assert configs, "no --mcp-config: the CLI would fall back to other scopes"
    assert all(json.loads(config) == {"mcpServers": {}} for config in configs)
    assert not any("write_query" in token for token in cmd)


def test_restrict_replaces_every_mcp_config_value() -> None:
    cmd = ["claude", "-p", "x", "--mcp-config", "/a.json", "/b.json",
           "--add-dir", "/p", "--mcp-config=/c.json"]

    restricted = manager_mod.restrict_to_read_only_tools(cmd)

    assert "/a.json" not in restricted and "/b.json" not in restricted
    assert not any("/c.json" in token for token in restricted)
    assert restricted[restricted.index("--add-dir") + 1] == "/p"


def _theforge_db(tmp_path: Path) -> Path:
    """A scratch TheForge built from the repo's own schema.sql."""
    db_path = tmp_path / "theforge.db"
    conn = sqlite3.connect(db_path)
    conn.executescript((REPO_ROOT / "schema.sql").read_text(encoding="utf-8"))
    conn.close()
    return db_path


def _planner_returning(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, result_text: str,
) -> None:
    import contextlib

    @contextlib.contextmanager
    def fake_build_cli_command(*args, **kwargs):
        yield ["fake-claude", "--mcp-config", str(MCP_CONFIG)]

    async def fake_run_agent(cmd):
        return {"success": True, "cost": 0.0, "duration": 0.0,
                "result_text": result_text}

    def connect(write: bool = False):
        return sqlite3.connect(db_path)

    monkeypatch.setattr(manager_mod, "build_planner_prompt", lambda *a, **k: "p")
    monkeypatch.setattr(manager_mod, "build_evaluator_prompt", lambda *a, **k: "e")
    monkeypatch.setattr(manager_mod, "build_cli_command", fake_build_cli_command)
    monkeypatch.setattr(manager_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(manager_mod, "get_role_turns", lambda *a, **k: 10)
    monkeypatch.setattr(manager_mod, "get_db_connection", connect)


def _task_rows(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT id, project_id, title, description, status, priority "
            "FROM tasks ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_manager_creates_the_planners_tasks_in_the_goal_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _theforge_db(tmp_path)
    plan = [
        {"title": "First", "description": "done when A", "priority": "critical",
         "project_id": 77, "status": "done"},
        {"title": "Second", "description": "done when B", "priority": "high"},
    ]
    _planner_returning(
        monkeypatch, db_path,
        "Planned.\nTASKS_JSON:\n```json\n" + json.dumps(plan) + "\n```\n",
    )

    _, task_ids = _run(manager_mod.run_planner_agent(
        "goal", 23, str(tmp_path), {}, SimpleNamespace(model="m"),
    ))

    rows = _task_rows(db_path)
    assert task_ids == [row[0] for row in rows]
    assert [row[1:] for row in rows] == [
        (23, "First", "done when A", "todo", "critical"),
        (23, "Second", "done when B", "todo", "high"),
    ]


@pytest.mark.parametrize(
    "block",
    [
        "TASKS_JSON: [{\"title\": \"x\", ",
        "TASKS_JSON: {\"title\": \"x\"}",
        "TASKS_JSON: [{\"title\": \"ok\"}, {\"title\": \"\"}]",
        "TASKS_JSON: [{\"title\": \"ok\", \"priority\": \"urgent\"}]",
    ],
    ids=["truncated", "not-a-list", "empty-title", "bad-priority"],
)
def test_malformed_tasks_json_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: str,
) -> None:
    db_path = _theforge_db(tmp_path)
    _planner_returning(monkeypatch, db_path, block)

    _, task_ids = _run(manager_mod.run_planner_agent(
        "goal", 23, str(tmp_path), {}, SimpleNamespace(model="m"),
    ))

    assert task_ids == []
    assert _task_rows(db_path) == []


def test_evaluator_follow_ups_are_created_by_the_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _theforge_db(tmp_path)
    _planner_returning(
        monkeypatch, db_path,
        "GOAL_STATUS: needs_more\nEVALUATION: half\nBLOCKERS: none\n"
        'TASKS_JSON: [{"title": "Follow-up", "description": "rest", "priority": "high"}]',
    )

    _, parsed = _run(manager_mod.run_evaluator_agent(
        "goal", 23, str(tmp_path), {}, [], [], SimpleNamespace(model="m"),
    ))

    rows = _task_rows(db_path)
    assert parsed["goal_status"] == "needs_more"
    assert parsed["tasks_created"] == [rows[0][0]]
    assert rows[0][1:] == (23, "Follow-up", "rest", "todo", "high")


# ---------------------------------------------------------------------------
# IR-04: git / gh children get an allowlisted environment
# ---------------------------------------------------------------------------

def _git_child_environ(repo: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of a real git process started by ``git_run``.

    A shell alias reads ``/proc/<git pid>/environ`` ($PPID of the alias
    shell is git itself), exactly what an agent sharing the UID could read.
    """
    result = git_ops_mod.git_run(
        ["-c", "alias.dumpenv=!cat /proc/$PPID/environ", "dumpenv"], repo, env=env,
    )
    assert result.returncode == 0, result.stderr
    pairs = [item.partition("=") for item in result.stdout.split("\0") if item]
    return {key: value for key, _, value in pairs}


def test_git_child_environment_holds_no_orchestrator_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    for key, value in SECRET_SENTINELS.items():
        monkeypatch.setenv(key, value)

    environ = _git_child_environ(repo)

    leaked = {key for key, value in environ.items()
              if any(secret in value for secret in SECRET_SENTINELS.values())}
    assert not leaked, f"git child environment leaks {sorted(leaked)}"
    assert environ.get("PATH") == os.environ["PATH"]
    assert environ.get("GIT_NO_REPLACE_OBJECTS") == "1"


def test_git_child_environment_drops_repository_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    redirects = {
        "GIT_INDEX_FILE": str(tmp_path / "index"),
        "GIT_OBJECT_DIRECTORY": str(tmp_path / "objects"),
        "GIT_CONFIG_PARAMETERS": "'core.worktree'='/'",
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
    }
    for key, value in redirects.items():
        monkeypatch.setenv(key, value)

    environ = _git_child_environ(repo)

    assert not set(redirects) & set(environ), sorted(set(redirects) & set(environ))


def test_push_calls_get_github_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setenv("GH_TOKEN", SECRET_SENTINELS["GH_TOKEN"])
    monkeypatch.setenv("DATABASE_URL", SECRET_SENTINELS["DATABASE_URL"])

    environ = _git_child_environ(repo, env=git_ops_mod.github_credential_env())

    assert environ.get("GH_TOKEN") == SECRET_SENTINELS["GH_TOKEN"]
    assert "DATABASE_URL" not in environ


def test_gh_gets_github_credentials_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text("#!/bin/sh\nexec cat /proc/$$/environ\n", encoding="utf-8")
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    for key, value in SECRET_SENTINELS.items():
        monkeypatch.setenv(key, value)

    result = git_ops_mod._gh_run(["auth", "status"], tmp_path)

    pairs = [item.partition("=") for item in result.stdout.split("\0") if item]
    environ = {key: value for key, _, value in pairs}
    assert environ.get("GH_TOKEN") == SECRET_SENTINELS["GH_TOKEN"]
    assert environ.get("GITHUB_TOKEN") == SECRET_SENTINELS["GITHUB_TOKEN"]
    for key in ("DATABASE_URL", "PGPASSWORD", "ANTHROPIC_API_KEY",
                "EQUIPA_FAKE_SECRET_3132"):
        assert key not in environ, f"gh received {key}"
