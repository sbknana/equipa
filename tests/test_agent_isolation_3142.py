"""Task 3142 (ISO-4): pre-enable fixes for agent isolation.

Copyright 2026 Forgeborn

Findings of the independent isolation review (F1-F10, I1, I5) and
SECURITY-REVIEW-3140 (R3140-01). Each test fails on main before the fix:

* F3 / R3140-01: the import's link check is linear in the target length
  and bounded (target size, links per tree, components walked).
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from equipa import agent_launcher, isolation

REPO_ROOT = Path(__file__).resolve().parent.parent
_GIT = shutil.which("git") or "/usr/bin/git"


def _git(*args: str, cwd: Path, env: dict[str, str] | None = None,
         stdin: bytes | None = None) -> str:
    return subprocess.run(
        [_GIT, "-c", "user.name=Orchestrator", "-c",
         "user.email=o@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, input=stdin,
        env={**os.environ, **(env or {})},
    ).stdout.decode().strip()


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Path]:
    """A main checkout on ``main`` and a linked task worktree."""
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    (main / ".gitignore").write_text(".equipa-artifacts/\n")
    (main / "README").write_text("base\n")
    (main / "docs").mkdir()
    (main / "docs" / "index.md").write_text("docs\n")
    _git("add", "-A", cwd=main)
    _git("commit", "-q", "-m", "base", cwd=main)
    worktree = tmp_path / "worktrees" / "task-1"
    worktree.parent.mkdir()
    _git("worktree", "add", "-q", "-b", "forge-task-1", str(worktree), cwd=main)
    return {"main": main, "worktree": worktree}


def _commit_with_links(worktree: Path, tmp_path: Path,
                       links: dict[str, bytes]) -> str:
    """A commit on top of the worktree's HEAD that adds ``links`` (path ->
    raw target) with git plumbing, as an agent can in its clone: no file
    system link is ever made, so no PATH_MAX applies."""
    index = tmp_path / "plumbing.index"
    env = {"GIT_INDEX_FILE": str(index)}
    _git("read-tree", "HEAD", cwd=worktree, env=env)
    blobs: dict[bytes, str] = {}
    for path, target in links.items():
        if target not in blobs:
            blobs[target] = _git("hash-object", "-w", "--stdin",
                                 cwd=worktree, stdin=target)
        _git("update-index", "--add", "--cacheinfo",
             f"120000,{blobs[target]},{path}", cwd=worktree, env=env)
    tree = _git("write-tree", cwd=worktree, env=env)
    index.unlink()
    return _git("commit-tree", tree, "-p", "HEAD", "-m", "links", cwd=worktree)


UNIT = "equipa-agent-1-1-0123456789abcdef"


def _settings(tmp_path: Path, **overrides) -> isolation.IsolationSettings:
    exchange = tmp_path / "exchange"
    exchange.mkdir(exist_ok=True)
    section = {"exchange_dir": str(exchange), "git_executable": _GIT,
               "python": sys.executable, **overrides}
    return isolation.load_isolation_settings({"agent_isolation": section})


def _export_clone(repo: dict[str, Path], tmp_path: Path, prepare) -> tuple:
    """Build the agent's clone as the launcher does, let ``prepare(clone)``
    change it, export it, and return what the import needs."""
    settings = _settings(tmp_path)
    info = isolation.describe_worktree(str(repo["worktree"]))
    bundle = tmp_path / "handoff.bundle"
    handoff = isolation.build_handoff(
        ["claude", "-p", "x"], str(repo["worktree"]),
        {"PATH": os.environ["PATH"]}, settings, UNIT, info, "tok", bundle)
    handoff.header["cgroup"]["path"] = f"/app.slice/{UNIT}.scope"
    session = agent_launcher._IsolatedSession(handoff.header)
    home = tmp_path / "agent-home"
    home.mkdir()
    session.home = str(home)
    with open(bundle, "rb") as source:
        session.receive_workspace(source.fileno(), bundle.stat().st_size)
    prepare(Path(session.repo_dir))
    session.export()
    session.discard()
    return info, handoff, settings


def _import(info, handoff, settings) -> str:
    return isolation.import_agent_export(
        info, UNIT, handoff.export_path, settings.max_export_bytes)


def _check(worktree: Path, state: str) -> None:
    info = isolation.describe_worktree(str(worktree))
    isolation.check_imported_links(info, state, ())


def _finishes_within(seconds: float, action) -> tuple[bool, BaseException | None]:
    """Run ``action`` in a daemon thread; (finished in time, its error).

    A thread, so the old quadratic walk fails this test instead of hanging
    the suite for hours."""
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            action()
        except BaseException as exc:  # noqa: BLE001 - handed to the test
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        return False, None
    return True, outcome[0]


# --- F3 / R3140-01: the link check is linear and bounded -------------------------------


def test_a_one_megabyte_link_target_is_refused_in_under_0_2_seconds(
        repo: dict[str, Path], tmp_path: Path) -> None:
    worktree = repo["worktree"]
    target = b"a/" * (512 * 1024)                     # 1 MiB of components
    state = _commit_with_links(worktree, tmp_path, {"docs/long": target})
    info = isolation.describe_worktree(str(worktree))
    started = time.perf_counter()
    finished, error = _finishes_within(
        5.0, lambda: isolation.check_imported_links(info, state, ()))
    elapsed = time.perf_counter() - started
    assert finished, "the link check of a 1 MiB target did not finish in 5 s"
    assert isinstance(error, isolation.AgentIsolationError)
    assert "limit 4095" in str(error)
    assert elapsed < 0.2, f"refusing a 1 MiB link target took {elapsed:.2f}s"


def test_a_link_target_longer_than_path_max_is_refused(
        repo: dict[str, Path], tmp_path: Path) -> None:
    """A long target without a slash stays lexically inside the tree; the
    kernel could never create it, so the import refuses it."""
    state = _commit_with_links(repo["worktree"], tmp_path,
                               {"docs/long": b"x" * 5000})
    with pytest.raises(isolation.AgentIsolationError,
                       match=r"docs/long whose target is 5000 bytes"):
        _check(repo["worktree"], state)


def test_a_target_of_exactly_the_limit_is_walked(
        repo: dict[str, Path], tmp_path: Path) -> None:
    at_limit = b"./" * 2043 + b"/index.md"            # 4095 bytes
    assert len(at_limit) == 4095
    state = _commit_with_links(repo["worktree"], tmp_path,
                               {"docs/ok": at_limit})
    _check(repo["worktree"], state)
    over = b"./" * 2044 + b"index.md"                 # 4096 bytes
    state = _commit_with_links(repo["worktree"], tmp_path, {"docs/no": over})
    with pytest.raises(isolation.AgentIsolationError, match="limit 4095"):
        _check(repo["worktree"], state)


def test_a_tree_with_too_many_links_is_refused(
        repo: dict[str, Path], tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(isolation, "_MAX_TREE_SYMLINKS", 3)
    state = _commit_with_links(repo["worktree"], tmp_path,
                               {f"docs/l{i}": b"index.md" for i in range(4)})
    with pytest.raises(isolation.AgentIsolationError,
                       match="more than 3 symbolic links"):
        _check(repo["worktree"], state)


def test_many_long_links_are_checked_quickly(
        repo: dict[str, Path], tmp_path: Path) -> None:
    """1000 changed links (the limit) with targets at the size limit: the
    old walk took about 0.03 s per link per commit; now the components
    walked are capped for the whole check."""
    target = b"a/" * 2046 + b"x"                      # 4093 bytes, in tree
    state = _commit_with_links(
        repo["worktree"], tmp_path,
        {f"docs/l{i:04d}": target for i in range(1000)})
    info = isolation.describe_worktree(str(repo["worktree"]))
    finished, error = _finishes_within(
        20.0, lambda: isolation.check_imported_links(info, state, ()))
    assert finished, "checking 1000 long links did not finish in 20 s"
    assert isinstance(error, isolation.AgentIsolationError)
    assert "path components to check" in str(error)


def test_walk_budget_spans_every_link_of_the_check(
        repo: dict[str, Path], tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(isolation, "_MAX_LINK_WALK_STEPS", 50)
    target = b"a/" * 10 + b"x"                        # 11 components
    state = _commit_with_links(repo["worktree"], tmp_path,
                               {f"docs/l{i}": target for i in range(4)})
    _check(repo["worktree"], state)                   # 44 components
    state = _commit_with_links(repo["worktree"], tmp_path,
                               {f"docs/l{i}": target for i in range(5)})
    with pytest.raises(isolation.AgentIsolationError,
                       match="more than 50 path components"):
        _check(repo["worktree"], state)               # 55 components


def test_trie_walk_is_linear_in_the_directory_depth() -> None:
    """A link deep in the tree, reached through a target that walks down
    its whole directory chain: each component is one lookup."""
    depth = 1000
    deep = "/".join(["d"] * depth)
    # l, at the bottom of the chain, points back up to the tree root.
    links = {f"{deep}/l": "../" * depth, "start": "x"}
    trie = isolation._LinkTrie.build(links)
    calls: list[str] = []

    def lookup(path: str) -> str | None:
        calls.append(path)
        return links.get(path)

    started = time.perf_counter()
    assert not isolation._link_escapes("start", f"{deep}/l/README", lookup,
                                       links=trie)
    assert isolation._link_escapes("start", f"{deep}/l/..", lookup, links=trie)
    assert time.perf_counter() - started < 0.05
    # Only real links are looked up by path.
    assert calls == [f"{deep}/l", f"{deep}/l"]


def test_import_check_walks_every_link_through_the_trie(
        repo: dict[str, Path], tmp_path: Path, monkeypatch) -> None:
    """check_imported_links must hand _link_escapes the tree's links as a
    trie and the shared budget: without them the walk falls back to joining
    the directory path for every component, which is quadratic again."""
    state = _commit_with_links(repo["worktree"], tmp_path,
                               {"docs/a": b"index.md", "docs/b": b"a",
                                "top": b"docs/b"})
    real = isolation._link_escapes
    seen: list[tuple[str, object, object]] = []

    def spy(path, target, link_target=None, *, links=None, budget=None):
        seen.append((path, links, budget))
        return real(path, target, link_target, links=links, budget=budget)

    monkeypatch.setattr(isolation, "_link_escapes", spy)
    _check(repo["worktree"], state)
    assert sorted(path for path, _links, _budget in seen) == \
        ["docs/a", "docs/b", "top"]
    tries = {id(links) for _path, links, _budget in seen}
    budgets = {id(budget) for _path, _links, budget in seen}
    assert len(tries) == 1 and len(budgets) == 1, "one trie, one budget"
    trie = seen[0][1]
    assert isinstance(trie, isolation._LinkTrie)
    assert trie.children["docs"].children["b"].link == "docs/b"
    assert trie.children["top"].link == "top"
    assert isinstance(seen[0][2], isolation._WalkBudget)


def test_trie_walk_agrees_with_the_path_walk() -> None:
    links = {"sub/a": "..", "x": "y", "y": "x", "docs/up": "..",
             "abs": "/usr/bin/python3", "deep/er/l": "../../docs"}
    trie = isolation._LinkTrie.build(links)
    cases = [("sub/b", "a/.."), ("sub/b", "a/../.."), ("z", "x"),
             ("new", "abs"), ("sub/c", "a/docs"), ("docs/b", "up/README"),
             ("docs/b", "up/.."), ("q", "deep/er/l/../.."),
             ("q", "deep/er/l/index.md"), ("q", "nope/../../x")]
    for path, target in cases:
        assert isolation._link_escapes(path, target, links.get, links=trie) \
            == isolation._link_escapes(path, target, links.get), (path, target)


# --- F5: the imported tip must descend from the dispatch base ----------------------------


def _second_base_commit(repo: dict[str, Path]) -> str:
    """Give the task branch a second commit before dispatch; returns it."""
    worktree = repo["worktree"]
    (worktree / "progress.txt").write_text("earlier attempt\n")
    _git("add", "progress.txt", cwd=worktree)
    _git("commit", "-q", "-m", "earlier attempt", cwd=worktree)
    return _git("rev-parse", "HEAD", cwd=worktree)


def test_import_refuses_a_rewound_task_branch(
        repo: dict[str, Path], tmp_path: Path) -> None:
    base = _second_base_commit(repo)

    def rewind(clone: Path) -> None:
        _git("reset", "-q", "--hard", "HEAD~1", cwd=clone)

    exported = _export_clone(repo, tmp_path, rewind)
    with pytest.raises(isolation.AgentIsolationError,
                       match="does not descend from the dispatch base"):
        _import(*exported)
    assert _git("rev-parse", "forge-task-1", cwd=repo["main"]) == base


def test_import_refuses_an_unrelated_task_branch_tip(
        repo: dict[str, Path], tmp_path: Path) -> None:
    base = _git("rev-parse", "forge-task-1", cwd=repo["main"])

    def replace(clone: Path) -> None:
        tree = _git("write-tree", cwd=clone)
        orphan = _git("commit-tree", tree, "-m", "unrelated history", cwd=clone)
        _git("reset", "-q", "--soft", orphan, cwd=clone)

    exported = _export_clone(repo, tmp_path, replace)
    with pytest.raises(isolation.AgentIsolationError,
                       match="does not descend from the dispatch base"):
        _import(*exported)
    assert _git("rev-parse", "forge-task-1", cwd=repo["main"]) == base


def test_import_accepts_a_tip_that_descends_from_the_base(
        repo: dict[str, Path], tmp_path: Path) -> None:
    base = _second_base_commit(repo)

    def commit(clone: Path) -> None:
        (clone / "new.txt").write_text("agent work\n")
        _git("add", "new.txt", cwd=clone)
        _git("commit", "-q", "-m", "agent work", cwd=clone)

    tip = _import(*_export_clone(repo, tmp_path, commit))
    assert tip != base
    assert _git("rev-parse", f"{tip}~1", cwd=repo["main"]) == base


def test_import_accepts_an_unchanged_tip(repo: dict[str, Path],
                                         tmp_path: Path) -> None:
    base = _git("rev-parse", "forge-task-1", cwd=repo["main"])
    assert _import(*_export_clone(repo, tmp_path, lambda clone: None)) == base


# --- F1: isolation is sticky once the operator enables it -------------------------------


@pytest.fixture
def host(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    """The host-level state: the operator's marker (absent until a test
    creates it) and the host dispatch config (absent until written)."""
    from equipa import config

    state = SimpleNamespace(marker=tmp_path / "etc" / "require-agent-isolation",
                            config=tmp_path / "host" / "dispatch_config.json")
    state.marker.parent.mkdir()
    state.config.parent.mkdir()
    monkeypatch.setattr(isolation, "REQUIRED_MARKER", state.marker,
                        raising=False)
    monkeypatch.setattr(config, "host_dispatch_config_path",
                        lambda: state.config, raising=False)
    # Nothing may fall back to a dispatch config the test runner happens
    # to have in its working directory.
    monkeypatch.chdir(tmp_path)
    return state


_SECTION = {"exchange_dir": "/var/lib/equipa-agent/exchange",
            "agent_user": "equipa-agent"}


def _write_json(path: Path, data) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_marker_turns_isolation_on_whatever_the_config_says(host) -> None:
    off = {"features": {"agent_isolation": False}}
    assert isolation.isolation_enabled(off) is False
    host.marker.write_text("")
    assert isolation.isolation_enabled(off) is True
    assert isolation.isolation_enabled({}) is True
    assert isolation.unisolated_spawn_refusal("RLM") is not None
    assert isolation.worktree_execution_refusal("npm install") is not None


def test_a_marker_that_cannot_be_checked_counts_as_present(
        host, tmp_path: Path, monkeypatch) -> None:
    closed = tmp_path / "closed"
    closed.mkdir(mode=0o000)
    try:
        monkeypatch.setattr(isolation, "REQUIRED_MARKER", closed / "marker",
                            raising=False)
        if os.access(closed, os.X_OK):  # root ignores the mode
            pytest.fail("cannot build an unsearchable directory as this user")
        assert isolation.isolation_required() is True
    finally:
        closed.chmod(0o700)


@pytest.mark.parametrize("config", [
    {},
    {"features": {}},
    {"features": {"agent_isolation": False}},
    {"features": {"agent_isolation": "false"}, "agent_isolation": _SECTION},
])
def test_required_isolation_refuses_a_config_that_turns_it_off(
        host, config) -> None:
    assert isolation.isolation_requirement_refusal(config) is None
    host.marker.write_text("")
    refusal = isolation.isolation_requirement_refusal(config)
    assert refusal is not None
    assert "required on this host" in refusal
    assert str(host.marker) in refusal


def test_required_isolation_accepts_a_config_that_turns_it_on(host) -> None:
    host.marker.write_text("")
    config = {"features": {"agent_isolation": True}, "agent_isolation": _SECTION}
    assert isolation.isolation_requirement_refusal(config) is None
    # An unreadable config reads the flag ON (fail-closed); its missing
    # settings refuse the spawn instead.
    assert isolation.isolation_requirement_refusal(
        {"_config_load_error": "x: truncated"}) is None


def test_spawn_refuses_when_required_and_the_config_turns_it_off(
        host, monkeypatch) -> None:
    host.marker.write_text("")
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    with pytest.raises(isolation.AgentIsolationError,
                       match="required on this host"):
        asyncio.run(isolation.spawn_isolated_agent(
            ["claude"], None, {},
            {"features": {"agent_isolation": False},
             "agent_isolation": _SECTION}))


def test_agent_runner_never_spawns_unisolated_while_required(
        host, monkeypatch, tmp_path: Path) -> None:
    """The real spawn path: with the marker and a config that turns the
    flag off, the dispatch is refused and no process is started."""
    from equipa import agent_runner
    from equipa import config as equipa_config

    host.marker.write_text("")
    equipa_config.set_active_dispatch_config(
        {"features": {"agent_isolation": False}})
    started: list = []

    async def no_exec(*args, **kwargs):
        started.append(args)
        raise AssertionError("an agent process was started")

    monkeypatch.setattr(agent_runner.asyncio, "create_subprocess_exec", no_exec)
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    try:
        with pytest.raises(agent_runner.AgentDispatchRefused,
                           match="required on this host"):
            asyncio.run(agent_runner._spawn_agent_process(
                ["claude", "-p", "x"], str(tmp_path)))
    finally:
        equipa_config.set_active_dispatch_config(None)
    assert started == []


def test_per_run_config_without_the_key_keeps_host_isolation(
        host, tmp_path: Path) -> None:
    from equipa.config import is_feature_enabled, load_dispatch_config

    _write_json(host.config, {"features": {"agent_isolation": True},
                              "agent_isolation": _SECTION})
    per_run = _write_json(tmp_path / "retry.json", {"max_retries": 2})
    config = load_dispatch_config(per_run)
    assert config["max_retries"] == 2
    assert is_feature_enabled(config, "agent_isolation") is True
    assert config["agent_isolation"] == _SECTION
    # Other flags are still the per-run file's (here: the defaults).
    assert is_feature_enabled(config, "hooks") is False


def test_per_run_config_cannot_turn_host_isolation_off(
        host, tmp_path: Path, caplog) -> None:
    from equipa.config import is_feature_enabled, load_dispatch_config

    _write_json(host.config, {"features": {"agent_isolation": True},
                              "agent_isolation": _SECTION})
    per_run = _write_json(tmp_path / "off.json",
                          {"features": {"agent_isolation": False,
                                        "hooks": True}})
    with caplog.at_level("WARNING", logger="equipa.config"):
        config = load_dispatch_config(per_run)
    assert is_feature_enabled(config, "agent_isolation") is True
    assert is_feature_enabled(config, "hooks") is True
    assert "cannot turn agent isolation off" in caplog.text


def test_a_missing_per_run_config_does_not_turn_isolation_off(
        host, tmp_path: Path) -> None:
    from equipa.cli import warn_missing_dispatch_config
    from equipa.config import is_feature_enabled, load_dispatch_config

    _write_json(host.config, {"features": {"agent_isolation": True},
                              "agent_isolation": _SECTION})
    mistyped = tmp_path / "mistyped.json"
    config = load_dispatch_config(mistyped)
    assert is_feature_enabled(config, "agent_isolation") is True
    assert config["agent_isolation"] == _SECTION
    # The CLI names the missing path; an absent or existing one is quiet.
    warning = warn_missing_dispatch_config(str(mistyped))
    assert warning is not None and "does not exist" in warning
    assert str(mistyped) in warning
    assert warn_missing_dispatch_config(None) is None
    assert warn_missing_dispatch_config(str(host.config)) is None


def test_loading_a_missing_config_writes_nothing_to_stdout(
        host, tmp_path: Path, capsys) -> None:
    """stdout is the MCP server's JSON-RPC channel, and every
    get_active_dispatch_config() without a registered config loads the
    usually absent repo-root file: nothing may be printed there."""
    from equipa import config

    config.load_dispatch_config(tmp_path / "absent.json")
    config.set_active_dispatch_config(None)
    config.get_active_dispatch_config()
    assert capsys.readouterr().out == ""


def test_cli_warns_about_a_missing_dispatch_config_before_loading_it() -> None:
    import ast

    tree = ast.parse((REPO_ROOT / "equipa" / "cli.py").read_text(
        encoding="utf-8"))
    main = next(node for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "async_main")
    lines = {ast.unparse(node): node.lineno for node in ast.walk(main)
             if isinstance(node, ast.Call)}
    warned = lines["warn_missing_dispatch_config(args.dispatch_config)"]
    printed = lines["print(missing_config)"]
    loaded = lines["load_dispatch_config(args.dispatch_config)"]
    assert warned < printed < loaded


def test_per_run_config_keeps_its_own_isolation_section(
        host, tmp_path: Path) -> None:
    from equipa.config import load_dispatch_config

    _write_json(host.config, {"features": {"agent_isolation": True},
                              "agent_isolation": _SECTION})
    own = {**_SECTION, "pids_max": 256}
    per_run = _write_json(tmp_path / "own.json", {"agent_isolation": own})
    assert load_dispatch_config(per_run)["agent_isolation"] == own


def test_per_run_config_may_turn_isolation_on(host, tmp_path: Path) -> None:
    from equipa.config import is_feature_enabled, load_dispatch_config

    _write_json(host.config, {"features": {"agent_isolation": False}})
    per_run = _write_json(tmp_path / "on.json",
                          {"features": {"agent_isolation": True}})
    assert is_feature_enabled(load_dispatch_config(per_run),
                              "agent_isolation") is True
    plain = _write_json(tmp_path / "plain.json", {"max_retries": 2})
    assert is_feature_enabled(load_dispatch_config(plain),
                              "agent_isolation") is False


def test_an_unreadable_host_config_keeps_isolation_on(
        host, tmp_path: Path) -> None:
    from equipa.config import is_feature_enabled, load_dispatch_config

    host.config.write_text('{"features": {"agent_isolation": tr',
                           encoding="utf-8")
    per_run = _write_json(tmp_path / "retry.json", {"max_retries": 2})
    assert is_feature_enabled(load_dispatch_config(per_run),
                              "agent_isolation") is True


def test_carrying_isolation_keeps_other_gates_fail_closed(
        host, tmp_path: Path) -> None:
    """A per-run ``features`` that is not an object reads every fail-closed
    gate as ON; carrying isolation must not replace it with a dict that
    turns the others off."""
    from equipa.config import is_feature_enabled, load_dispatch_config

    _write_json(host.config, {"features": {"agent_isolation": True},
                              "agent_isolation": _SECTION})
    per_run = _write_json(tmp_path / "odd.json", {"features": ["hooks"]})
    config = load_dispatch_config(per_run)
    assert is_feature_enabled(config, "bash_security_pretooluse") is True
    assert is_feature_enabled(config, "agent_isolation") is True


def test_carry_does_not_touch_the_shared_default_flags(
        host, tmp_path: Path) -> None:
    from equipa.config import DEFAULT_DISPATCH_CONFIG, load_dispatch_config

    _write_json(host.config, {"features": {"agent_isolation": True},
                              "agent_isolation": _SECTION})
    load_dispatch_config(_write_json(tmp_path / "x.json", {"max_retries": 1}))
    assert DEFAULT_DISPATCH_CONFIG["features"]["agent_isolation"] is False


def test_startup_line_names_the_isolation_state(host) -> None:
    assert isolation.describe_isolation_state({}) == "agent_isolation: OFF"
    assert isolation.describe_isolation_state(
        {"features": {"agent_isolation": True}}) == "agent_isolation: ON"
    host.marker.write_text("")
    assert isolation.describe_isolation_state({}).startswith(
        "agent_isolation: ON (required by ")


def test_cli_refuses_before_any_mode_runs() -> None:
    """async_main checks the requirement right after loading the config and
    before it selects a mode handler."""
    source = (REPO_ROOT / "equipa" / "cli.py").read_text(encoding="utf-8")
    body = source[source.index("async def async_main"):]
    body = body[:body.index("\ndef ")]
    loaded = body.index("args.dispatch_config = load_dispatch_config(")
    checked = body.index("isolation_requirement_refusal(args.dispatch_config)")
    refused = body.index("refuse_dispatch(isolation_refusal)")
    handler = body.index("_select_mode_handler(args)")
    assert loaded < checked < refused < handler


# --- F4: with one shared agent UID, isolated units run one at a time --------------------


@pytest.fixture
def lock_dir(tmp_path: Path, monkeypatch) -> Path:
    """spawn_isolated_agent with its setup faked, a private lock directory,
    fast polling and no live agent scopes (never the real /run/user)."""
    directory = tmp_path / "run"
    directory.mkdir(mode=0o700)
    settings = _settings(tmp_path)
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    monkeypatch.setattr(isolation, "load_isolation_settings",
                        lambda config: settings)
    monkeypatch.setattr(isolation, "resolve_agent_identity", lambda s: None)
    monkeypatch.setattr(isolation, "check_host", lambda s, i: None)
    monkeypatch.setattr(isolation, "_runtime_dir", lambda: str(directory))
    monkeypatch.setattr(isolation, "_unit_lock_dir", lambda: str(directory),
                        raising=False)
    monkeypatch.setattr(isolation, "_SLOT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(isolation, "live_agent_scopes",
                        lambda app_slice=None: [])
    monkeypatch.setattr(isolation, "sweep_stale_scopes",
                        lambda app_slice=None: [])

    class _Process:
        pid = os.getpid()
        stdin = None
        returncode = 0

    async def fake_spawn(cmd, cwd, env, settings, unit, slot, limit):
        agent = isolation.IsolatedAgent(_Process(), unit, settings, None,
                                        None, slot)
        return agent.process, agent

    monkeypatch.setattr(isolation, "_spawn_in_slot", fake_spawn)
    return directory


async def _spawn_as(role: str | None):
    with isolation.unit_role(role):
        return await isolation.spawn_isolated_agent(["claude"], None, {}, {})


async def _started_within(awaitable, seconds: float = 0.3):
    task = asyncio.ensure_future(awaitable)
    await asyncio.sleep(seconds)
    return task


@pytest.mark.parametrize("first,second", [
    ("developer", "developer"),
    ("developer", "tester"),
    ("tester", "integration-tester"),
    ("evaluator", None),
])
def test_two_ordinary_units_never_overlap(lock_dir, first, second) -> None:
    """Task B's developer may not start while task A's developer or tester
    runs: it could replace A's export or edit A's tester clone."""
    async def scenario() -> bool:
        _process, running = await _spawn_as(first)
        waiting = await _started_within(_spawn_as(second))
        overlapped = waiting.done()
        running.release()
        _process, later = await asyncio.wait_for(waiting, 2)
        later.release()
        return overlapped

    assert asyncio.run(scenario()) is False, \
        f"a {second or 'helper'} unit ran beside a {first} unit"


def test_units_of_another_process_are_waited_for(lock_dir) -> None:
    """The units lock is per user: a unit another orchestrator process
    holds (shared, as the old code took it) delays every unit here."""
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys\n"
         "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
         "fcntl.flock(fd, fcntl.LOCK_SH)\n"
         "print('held', flush=True)\n"
         "sys.stdin.read()\n",
         str(lock_dir / isolation._UNITS_LOCK_NAME)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"

        async def scenario() -> bool:
            waiting = await _started_within(_spawn_as("developer"))
            overlapped = waiting.done()
            holder.stdin.close()
            _process, agent = await asyncio.wait_for(waiting, 5)
            agent.release()
            return overlapped

        assert asyncio.run(scenario()) is False
    finally:
        holder.kill()
        holder.wait()


def test_every_role_runs_alone_until_units_have_their_own_uid(
        monkeypatch) -> None:
    roles = ["developer", "tester", "integration-tester", "evaluator",
             "debugger", None, "security-reviewer", "code-reviewer"]
    assert all(isolation.unit_runs_alone(role) for role in roles)
    # Once a UID pool exists, only the reviewers keep running alone.
    monkeypatch.setattr(isolation, "PER_UNIT_UIDS", True)
    assert [role for role in roles if isolation.unit_runs_alone(role)] == \
        ["security-reviewer", "code-reviewer"]


@pytest.mark.parametrize("value,enabled,refused", [
    (2, True, True), (8, True, True), (1, True, False),
    (4, False, False), (True, True, False), ("4", True, False),
])
def test_concurrency_above_one_is_refused_with_isolation_on(
        monkeypatch, value, enabled, refused) -> None:
    monkeypatch.setattr(isolation, "isolation_enabled",
                        lambda config=None: enabled)
    refusal = isolation.concurrency_refusal(value, "--max-concurrent")
    assert (refusal is not None) is refused
    if refused:
        assert f"--max-concurrent {value} refused" in refusal
        assert "set max_concurrent to 1" in refusal


@pytest.mark.parametrize("cli_value,config_value", [
    (4, None), (None, 2), (None, None),
])
def test_parallel_tasks_refuse_a_cap_above_one_with_isolation_on(
        monkeypatch, cli_value, config_value) -> None:
    from equipa import dispatch

    monkeypatch.setattr(isolation, "isolation_enabled",
                        lambda config=None: True)
    config = {} if config_value is None else {"max_concurrent": config_value}
    args = SimpleNamespace(max_concurrent=cli_value, dispatch_config=config)
    with pytest.raises(dispatch.DispatchRefused) as refused:
        dispatch.resolve_max_concurrent(args)
    assert "set max_concurrent to 1" in refused.value.message


def test_parallel_tasks_accept_one_with_isolation_on(monkeypatch) -> None:
    from equipa import dispatch

    monkeypatch.setattr(isolation, "isolation_enabled",
                        lambda config=None: True)
    args = SimpleNamespace(max_concurrent=None,
                           dispatch_config={"max_concurrent": 1})
    assert dispatch.resolve_max_concurrent(args) == 1
    monkeypatch.setattr(isolation, "isolation_enabled",
                        lambda config=None: False)
    args = SimpleNamespace(max_concurrent=4, dispatch_config={})
    assert dispatch.resolve_max_concurrent(args) == 4


def test_auto_run_and_parallel_goals_refuse_a_cap_above_one(
        monkeypatch) -> None:
    from equipa import dispatch

    monkeypatch.setattr(isolation, "isolation_enabled",
                        lambda config=None: True)
    with pytest.raises(dispatch.DispatchRefused):
        asyncio.run(dispatch.run_auto_dispatch(
            [], {"max_concurrent": 3}, SimpleNamespace()))
    with pytest.raises(dispatch.DispatchRefused):
        asyncio.run(dispatch.run_parallel_goals(
            [], {"max_concurrent": 3}, SimpleNamespace(max_concurrent=None)))


def test_every_dispatch_semaphore_is_gated_by_the_concurrency_refusal() -> None:
    """Each place that runs tasks side by side checks the cap first."""
    import ast

    tree = ast.parse((REPO_ROOT / "equipa" / "dispatch.py").read_text(
        encoding="utf-8"))
    gated = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [node for node in ast.walk(function)
                 if isinstance(node, ast.Call)]
        names = {(getattr(call.func, "attr", None)
                  or getattr(call.func, "id", None)): call.lineno
                 for call in calls}
        if "Semaphore" in names:
            refusal_line = min((call.lineno for call in calls
                                if getattr(call.func, "id", None)
                                in ("concurrency_refusal",
                                    "resolve_max_concurrent")),
                               default=None)
            assert refusal_line is not None \
                and refusal_line < names["Semaphore"], function.name
            gated.append(function.name)
    assert {"run_auto_dispatch", "run_parallel_goals",
            "run_parallel_tasks"} <= set(gated)


_UID_POOL = {"isolation_uid_pool": ["equipa-agent-1", "equipa-agent-2"]}


@pytest.mark.parametrize("config,refused", [
    ({"features": {"agent_isolation": True}, **_UID_POOL}, True),
    ({"features": {"agent_isolation": True}, "isolation_uid_pool": []}, True),
    ({"features": {"agent_isolation": True}}, False),
    ({"features": {"agent_isolation": False}, **_UID_POOL}, False),
])
def test_a_uid_pool_is_refused_while_isolation_is_on(
        host, config, refused) -> None:
    """The pool is not implemented, so naming one must never read as leave
    to run isolated agents side by side (review F4)."""
    refusal = isolation.uid_pool_refusal(config)
    assert (refusal is not None) is refused
    if refused:
        assert "isolation_uid_pool is set" in refusal
        assert "not implemented" in refusal
        assert "set max_concurrent to 1" in refusal


def test_isolation_settings_and_spawn_refuse_a_uid_pool(
        host, monkeypatch) -> None:
    """Every isolated launch loads its settings, so the pool is refused at
    the spawn as well, whatever max_concurrent says."""
    config = {"features": {"agent_isolation": True},
              "agent_isolation": _SECTION, "max_concurrent": 1, **_UID_POOL}
    with pytest.raises(isolation.AgentIsolationError,
                       match="isolation_uid_pool is set"):
        isolation.load_isolation_settings(config)
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    with pytest.raises(isolation.AgentIsolationError,
                       match="isolation_uid_pool is set"):
        asyncio.run(isolation.spawn_isolated_agent(["claude"], None, {},
                                                   config))


def test_concurrency_refusal_names_the_unimplemented_uid_pool(
        monkeypatch) -> None:
    monkeypatch.setattr(isolation, "isolation_enabled",
                        lambda config=None: True)
    refusal = isolation.concurrency_refusal(2)
    assert refusal is not None and "isolation_uid_pool" in refusal


def test_cli_refuses_a_uid_pool_before_any_mode_runs() -> None:
    """async_main refuses a config naming a UID pool with the same refusal
    as a config that turns required isolation off (calls, not comments)."""
    import ast

    tree = ast.parse((REPO_ROOT / "equipa" / "cli.py").read_text(
        encoding="utf-8"))
    main = next(node for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "async_main")
    # The refusal assigned to isolation_refusal includes the pool check.
    assigned = [node for node in ast.walk(main)
                if isinstance(node, ast.Assign)
                and [ast.unparse(t) for t in node.targets]
                == ["isolation_refusal"]
                and "uid_pool_refusal(args.dispatch_config)"
                in ast.unparse(node.value)]
    calls: dict[str, list[ast.Call]] = {}
    for node in ast.walk(main):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(
                node.func, "attr", None)
            calls.setdefault(name, []).append(node)
    refused = [call for call in calls.get("refuse_dispatch", [])
               if ast.unparse(call) == "refuse_dispatch(isolation_refusal)"]
    handler = calls["_select_mode_handler"]
    assert assigned and refused and handler
    assert assigned[0].lineno < refused[0].lineno < handler[0].lineno


# --- F2: no loopback or LAN access from the agent unit ----------------------------------


_DENIED = ("127.0.0.0/8 ::1/128 0.0.0.0/8 ::/128 169.254.0.0/16 fe80::/10 "
           "224.0.0.0/4 ff00::/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 "
           "fc00::/7")


def _property(command: list[str], name: str) -> str | None:
    prefix = f"--property={name}="
    values = [arg[len(prefix):] for arg in command if arg.startswith(prefix)]
    assert len(values) <= 1, values
    return values[0] if values else None


def test_unit_denies_loopback_link_local_multicast_and_private_ranges(
        tmp_path: Path) -> None:
    command = isolation.build_launch_command(_settings(tmp_path), UNIT)
    assert _property(command, "IPAddressDeny") == _DENIED
    # systemd-run of systemd 255 refuses the symbolic names.
    for symbolic in ("localhost", "link-local", "multicast", "any"):
        assert symbolic not in _property(command, "IPAddressDeny").split()
    # Everything else stays reachable: the agent needs the public API.
    assert _property(command, "IPAddressAllow") is None
    # The properties go to systemd-run, before the command it runs.
    assert command.index(f"--property=IPAddressDeny={_DENIED}") \
        < command.index("--")


def test_extra_denied_ranges_add_to_the_defaults(tmp_path: Path) -> None:
    settings = _settings(tmp_path,
                         ip_address_deny_extra=["100.64.0.0/10", "10.1.0.0/16"])
    command = isolation.build_launch_command(settings, UNIT)
    assert _property(command, "IPAddressDeny") == \
        f"{_DENIED} 100.64.0.0/10 10.1.0.0/16"
    with pytest.raises(isolation.AgentIsolationError,
                       match="invalid network 'localhost'"):
        _settings(tmp_path, ip_address_deny_extra=["localhost"])
    with pytest.raises(isolation.AgentIsolationError, match="unknown"):
        _settings(tmp_path, ip_address_deny=["10.0.0.0/8"])


def test_handoff_names_the_denied_ranges(repo: dict[str, Path],
                                         tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    info = isolation.describe_worktree(str(repo["worktree"]))
    handoff = isolation.build_handoff(
        ["claude", "-p", "x"], str(repo["worktree"]),
        {"PATH": os.environ["PATH"]}, settings, UNIT, info, "tok",
        tmp_path / "handoff.bundle")
    assert handoff.header["network"] == {"deny": _DENIED.split()}
    handoff.header["cgroup"]["path"] = f"/app.slice/{UNIT}.scope"
    session = agent_launcher._IsolatedSession(handoff.header)
    assert [str(net) for net in session.network_deny] == _DENIED.split()


def _launcher_header(tmp_path: Path, **overrides) -> dict:
    header = {
        "unit": UNIT, "argv": ["claude", "-p", "x"],
        "executable": sys.executable, "env": {"PATH": os.environ["PATH"]},
        "files": [], "workdir_sources": [],
        "identity": {"user": "equipa-agent", "orchestrator_uid": os.getuid(),
                     "privileged_groups": []},
        "cgroup": {"path": f"/app.slice/{UNIT}.scope", "pids_max": 64,
                   "memory_max": 256 * 1024 ** 2, "cpu_weight": 100},
        "deny_read": [], "deny_write": [], "must_execute": [], "must_read": [],
        "git": {"executable": _GIT, "hardening_args": [], "hardening_env": {}},
        "workspace": None, "grace": 1.0,
        "network": {"deny": _DENIED.split()},
    }
    header.update(overrides)
    return header


def test_launcher_refuses_while_loopback_is_reachable(tmp_path: Path) -> None:
    """Here, as on a user manager that cannot apply IPAddressDeny=, the
    launcher's own loopback listener is reachable: the run is refused."""
    session = agent_launcher._IsolatedSession(_launcher_header(tmp_path))
    with pytest.raises(agent_launcher.IsolationRefused,
                       match=r"can connect to 127\.0\.0\.1.*IPAddressDeny"):
        session._verify_denied_access()


def test_launcher_verify_runs_the_network_check(tmp_path: Path,
                                                monkeypatch) -> None:
    session = agent_launcher._IsolatedSession(_launcher_header(tmp_path))
    for name in ("_verify_identity", "_verify_cgroup", "_verify_no_scheduler",
                 "_verify_required_access"):
        monkeypatch.setattr(session, name, lambda: None)
    with pytest.raises(agent_launcher.IsolationRefused, match="IPAddressDeny"):
        session.verify()


def test_launcher_accepts_when_nothing_denied_is_reachable(
        tmp_path: Path, monkeypatch) -> None:
    probed: list = []

    def unreachable(networks, addresses=None, timeout=2.0):
        probed.append([str(net) for net in networks])
        return []

    monkeypatch.setattr(agent_launcher, "reachable_denied_addresses",
                        unreachable)
    session = agent_launcher._IsolatedSession(_launcher_header(tmp_path))
    session._verify_denied_access()
    assert probed == [_DENIED.split()]


def test_launcher_refuses_a_deny_list_without_loopback(tmp_path: Path) -> None:
    session = agent_launcher._IsolatedSession(
        _launcher_header(tmp_path, network={"deny": ["10.0.0.0/8"]}))
    with pytest.raises(agent_launcher.IsolationRefused,
                       match="does not deny loopback"):
        session._verify_denied_access()


@pytest.mark.parametrize("network", [
    {"deny": ["not-a-network"]}, {"deny": "127.0.0.0/8"}, ["127.0.0.0/8"]])
def test_launcher_rejects_a_malformed_network_field(tmp_path: Path,
                                                    network) -> None:
    with pytest.raises(agent_launcher.IsolationRefused):
        agent_launcher._IsolatedSession(_launcher_header(tmp_path,
                                                         network=network))


def test_probe_finds_a_reachable_listener_quickly() -> None:
    import ipaddress

    loopback = [ipaddress.ip_network("127.0.0.0/8")]
    started = time.perf_counter()
    assert agent_launcher.reachable_denied_addresses(
        loopback, ["127.0.0.1"]) == ["127.0.0.1"]
    assert time.perf_counter() - started < 1.0
    # An address outside the denied ranges is not probed at all, and one
    # this host does not have is skipped.
    assert agent_launcher.reachable_denied_addresses(
        loopback, ["192.0.2.10"]) == []
    private = [ipaddress.ip_network("10.0.0.0/8")]
    assert agent_launcher.reachable_denied_addresses(
        private, ["10.255.255.254"], timeout=0.5) == []


def test_probe_reports_an_address_that_never_answers_as_unreachable(
        monkeypatch) -> None:
    """A dropped SYN (an nftables drop rule) leaves the connect pending:
    after the timeout the address counts as unreachable."""
    import ipaddress
    import select

    class _Silent(socket.socket):
        def connect_ex(self, address):
            return errno.EINPROGRESS

    monkeypatch.setattr(agent_launcher.socket, "socket", _Silent)
    monkeypatch.setattr(select, "select",
                        lambda read, write, error, timeout: ([], [], []))
    started = time.perf_counter()
    assert agent_launcher.reachable_denied_addresses(
        [ipaddress.ip_network("127.0.0.0/8")], ["127.0.0.1"],
        timeout=0.3) == []
    assert time.perf_counter() - started < 1.0


def test_local_addresses_include_the_hosts_own_lan_address(
        tmp_path: Path, monkeypatch) -> None:
    fib = tmp_path / "fib_trie"
    fib.write_text(
        "Main:\n  +-- 0.0.0.0/0 3 0 5\n"
        "     |-- 127.0.0.1\n        /32 host LOCAL\n"
        "     |-- 192.168.7.20\n        /24 link UNICAST\n"
        "     |-- 192.168.7.21\n        /32 host LOCAL\n"
        "     |-- 10.9.8.7\n        /32 host LOCAL\n")
    inet6 = tmp_path / "if_inet6"
    inet6.write_text(
        "00000000000000000000000000000001 01 80 10 80       lo\n"
        "fe800000000000000000000000000001 02 40 20 80     eth0\n"
        "fd000000000000000000000000000005 02 40 00 80     eth0\n")
    monkeypatch.setattr(agent_launcher, "_FIB_TRIE", str(fib))
    monkeypatch.setattr(agent_launcher, "_IF_INET6", str(inet6))
    assert agent_launcher._local_addresses() == [
        "127.0.0.1", "192.168.7.21", "10.9.8.7", "::1", "fd00::5"]


def test_listening_loopback_ports_are_read_from_proc(
        tmp_path: Path, monkeypatch) -> None:
    tcp = tmp_path / "tcp"
    tcp.write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue\n"
        "   0: 0100007F:1538 00000000:0000 0A 00000000:00000000\n"   # 127.0.0.1:5432
        "   1: 00000000:18EB 00000000:0000 0A 00000000:00000000\n"   # 0.0.0.0:6379
        "   2: 1501A8C0:0050 00000000:0000 0A 00000000:00000000\n"   # 192.168.1.21:80
        "   3: 0100007F:2328 0100007F:A1B2 01 00000000:00000000\n")  # established
    tcp6 = tmp_path / "tcp6"
    tcp6.write_text(
        "  sl  local_address                         remote_address  st\n"
        "   0: 00000000000000000000000001000000:1F90 "
        "00000000000000000000000000000000:0000 0A\n"                # ::1:8080
        "   1: 0000000000000000FFFF00000100007F:2382 "
        "00000000000000000000000000000000:0000 0A\n")               # ::ffff:127.0.0.1:9090
    monkeypatch.setattr(isolation, "_PROC_NET_TCP", (str(tcp), str(tcp6)))
    assert isolation.listening_loopback_ports() == [5432, 6379, 8080, 9090]


def test_probe_command_lists_operator_and_listening_ports(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(isolation, "listening_loopback_ports", lambda: [6379])
    settings = _settings(tmp_path)
    command = isolation.build_probe_command(
        str(VERIFY_SCRIPT), settings, [], str(tmp_path), [5432, 6379])
    ports = [command[index + 1] for index, arg in enumerate(command)
             if arg == "--deny-port"]
    assert ports == ["5432", "6379"]
    assert "--deny-port)" in VERIFY_SCRIPT.read_text()


def test_verification_main_takes_loopback_ports(tmp_path: Path,
                                                monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(isolation, "get_active_dispatch_config", lambda: {
        "agent_isolation": {"exchange_dir": str(tmp_path)}})
    monkeypatch.setattr(isolation, "_outer_checks", lambda settings: [])

    def probe_command(probe, settings, repos, home, loopback_ports=()):
        seen["ports"] = list(loopback_ports)
        return ["claude", "--inside"]

    async def fake_probe(command, config):
        return "RESULT: PASS\n"

    monkeypatch.setattr(isolation, "build_probe_command", probe_command)
    monkeypatch.setattr(isolation, "_run_probe", fake_probe)
    assert isolation.verification_main(
        ["--verify-probe", str(VERIFY_SCRIPT), "--loopback-port", "5432",
         "--loopback-port", "6379"]) == 0
    assert seen["ports"] == [5432, 6379]
    with pytest.raises(SystemExit):
        isolation.verification_main(["--verify-probe", str(VERIFY_SCRIPT),
                                     "--loopback-port", "70000"])


VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_agent_isolation.sh"


def _run_inside(tmp_path: Path, *args: str) -> list[str]:
    """The verify script's inside checks as this (unisolated) user."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    for tool, body in (("sudo", "exit 1"),
                       ("crontab", "echo 'not allowed' >&2; exit 1"),
                       ("at", "echo 'not allowed' >&2; exit 1"),
                       ("loginctl", "echo no")):
        script = fake_bin / tool
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
    result = subprocess.run(
        ["bash", str(VERIFY_SCRIPT), "--inside", *args],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"})
    return result.stdout.splitlines()


def test_verify_script_fails_when_a_loopback_service_is_reachable(
        tmp_path: Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    try:
        lines = _run_inside(tmp_path, "--deny-port", str(port))
    finally:
        listener.close()
    assert any(line.startswith("FAIL agent can connect to 127.0.0.1 on "
                               f"port(s) {port}") for line in lines), lines
    assert lines[-1].startswith("RESULT: FAIL")


def test_verify_script_passes_an_unreachable_port(tmp_path: Path) -> None:
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()                                   # nothing listens there
    lines = _run_inside(tmp_path, "--deny-port", str(port))
    assert ("PASS agent cannot connect to 127.0.0.1 on any of 1 local "
            "service port(s)") in lines


# --- F7: the agent user has no sudo rights and no root-equivalent group -------------------


def _fake_sudo(tmp_path: Path, output: str, status: int) -> str:
    sudo = tmp_path / "fake-sudo"
    (tmp_path / "sudo-output").write_text(output)
    sudo.write_text(f"#!/bin/sh\ncat {tmp_path / 'sudo-output'}\n"
                    f"printf '%s\\n' \"$@\" > {tmp_path / 'sudo-args'}\n"
                    f"exit {status}\n")
    sudo.chmod(0o755)
    return str(sudo)


def test_agent_without_sudo_rights_passes(tmp_path: Path) -> None:
    sudo = _fake_sudo(tmp_path, "User equipa-agent is not allowed to run "
                                "sudo on host.\n", 0)
    isolation.check_agent_has_no_sudo(_settings(tmp_path, sudo=sudo))
    assert (tmp_path / "sudo-args").read_text().split() == \
        ["-n", "-l", "-U", "equipa-agent"]


@pytest.mark.parametrize("output, status, message", [
    ("User equipa-agent may run the following commands on host:\n"
     "    (ALL) NOPASSWD: /usr/bin/apt\n", 0, "has sudo rights"),
    ("sudo: a password is required\n", 1, "cannot verify"),
    ("Sorry, user orchestrator may not run sudo on host.\n", 1,
     "cannot verify"),
    ("", 0, "cannot verify"),
])
def test_agent_sudo_rights_or_an_unclear_answer_refuse(
        tmp_path: Path, output: str, status: int, message: str) -> None:
    sudo = _fake_sudo(tmp_path, output, status)
    with pytest.raises(isolation.AgentIsolationError, match=message):
        isolation.check_agent_has_no_sudo(_settings(tmp_path, sudo=sudo))


def test_a_missing_sudo_refuses(tmp_path: Path) -> None:
    with pytest.raises(isolation.AgentIsolationError, match="cannot verify"):
        isolation.check_agent_has_no_sudo(
            _settings(tmp_path, sudo=str(tmp_path / "no-sudo")))


class _Passwd:
    def __init__(self, uid: int, gid: int = 4242, home: str = "/home/agent"):
        self.pw_uid, self.pw_gid, self.pw_dir = uid, gid, home


def test_dispatch_refuses_an_agent_user_with_sudo_rights(
        tmp_path: Path, monkeypatch) -> None:
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: _Passwd(os.getuid() + 1))
    sudo = _fake_sudo(tmp_path, "User equipa-agent may run the following "
                                "commands on host:\n    (ALL) ALL\n", 0)
    with pytest.raises(isolation.AgentIsolationError, match="has sudo rights"):
        isolation.resolve_agent_identity(_settings(tmp_path, sudo=sudo))
    sudo = _fake_sudo(tmp_path, "User equipa-agent is not allowed to run "
                                "sudo on host.\n", 0)
    identity = isolation.resolve_agent_identity(_settings(tmp_path, sudo=sudo))
    assert identity.uid == os.getuid() + 1


@pytest.mark.parametrize("group", ["docker", "lxd", "sudo", "adm", "root"])
def test_root_equivalent_groups_cannot_be_configured_away(
        tmp_path: Path, group: str) -> None:
    settings = _settings(tmp_path, privileged_groups=[])
    assert group in settings.privileged_groups
    assert group in _settings(tmp_path, privileged_groups=["games"]
                              ).privileged_groups


def test_dispatch_refuses_a_supplementary_privileged_group(
        tmp_path: Path, monkeypatch) -> None:
    """Membership that only the group database's NSS view shows (not the
    group file's member list) still refuses: os.getgrouplist is asked."""
    import grp
    import pwd

    group = grp.getgrgid(os.getgid())
    monkeypatch.setattr(pwd, "getpwnam", lambda name: _Passwd(os.getuid() + 1))
    monkeypatch.setattr(os, "getgrouplist",
                        lambda name, gid: [4242, group.gr_gid])
    sudo = _fake_sudo(tmp_path, "User equipa-agent is not allowed to run "
                                "sudo on host.\n", 0)
    settings = _settings(tmp_path, privileged_groups=[group.gr_name], sudo=sudo)
    with pytest.raises(isolation.AgentIsolationError,
                       match=f"privileged group '{group.gr_name}'"):
        isolation.resolve_agent_identity(settings)


def test_verify_script_checks_the_root_equivalent_groups() -> None:
    text = VERIFY_SCRIPT.read_text()
    for group in ("docker", "lxd", "sudo", "adm"):
        assert f" {group} " in text.split("for group in", 1)[1].split(";")[0]


# --- I1: the sudoers rule is checked by content -------------------------------------------


def _listing(settings, *, runas: str | None = None, command: str | None = None,
             options: str = "!authenticate",
             defaults: str = "!use_pty, !pam_session, env_reset, !log_output",
             extra_all: bool = True) -> str:
    command = command or f"{settings.python} -I {settings.launcher} --isolated"
    text = ("Matching Defaults entries for orchestrator on host:\n"
            "    env_reset, use_pty\n\n"
            "Runas and Command-specific defaults for orchestrator:\n"
            f"    Defaults!EQUIPA_AGENT_LAUNCH {defaults}\n\n"
            "User orchestrator may run the following commands on host:\n\n")
    if extra_all:
        text += ("Sudoers entry: /etc/sudoers\n    RunAsUsers: ALL\n"
                 "    Options: !authenticate\n    Commands:\n\tALL\n\n")
    text += ("Sudoers entry: /etc/sudoers.d/equipa-agent\n"
             f"    RunAsUsers: {runas or settings.agent_user}\n"
             f"    Options: {options}\n    Commands:\n\t{command}\n")
    return text


def test_narrow_rule_is_recognised_by_content(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert isolation.narrow_sudoers_rule_problems(
        settings, _listing(settings)) == []
    # Alias shown unexpanded is accepted too.
    assert isolation.narrow_sudoers_rule_problems(
        settings, _listing(settings, command="EQUIPA_AGENT_LAUNCH")) == []


@pytest.mark.parametrize("change, problem", [
    ({"runas": "ALL"}, "no sudoers rule"),
    ({"command": "ALL"}, "no sudoers rule"),
    ({"command": "/usr/bin/python3 -I /x/agent_launcher.py *"},
     "no sudoers rule"),
    ({"options": ""}, "not NOPASSWD"),
    ({"defaults": "!use_pty, env_reset"}, "lack !pam_session"),
    ({"defaults": "!pam_session"}, "lack !use_pty"),
])
def test_an_incomplete_rule_is_reported(tmp_path: Path, change: dict,
                                        problem: str) -> None:
    settings = _settings(tmp_path)
    problems = isolation.narrow_sudoers_rule_problems(
        settings, _listing(settings, **change))
    assert any(problem in item for item in problems), problems


def test_an_all_rule_alone_does_not_pass_the_outer_check(
        tmp_path: Path, monkeypatch) -> None:
    """I1: with (ALL) NOPASSWD: ALL, sudo -l -u agent <launcher> succeeds
    without the narrow rule; the outer check must still fail."""
    settings = _settings(tmp_path, secret_scan_roots=[str(tmp_path)])
    monkeypatch.setattr(isolation, "THEFORGE_DB", tmp_path / "missing.db")
    monkeypatch.setattr(isolation, "MCP_CONFIG", tmp_path / "no-mcp.json")
    monkeypatch.setattr(isolation, "_exit_status", lambda argv: 0)
    only_all = ("User orchestrator may run the following commands on host:\n\n"
                "Sudoers entry: /etc/sudoers\n    RunAsUsers: ALL\n"
                "    Options: !authenticate\n    Commands:\n\tALL\n")
    monkeypatch.setattr(isolation, "sudoers_listing",
                        lambda settings: (only_all, 0), raising=False)
    failures = "\n".join(isolation._outer_checks(settings))
    assert "the sudoers rule is missing or incomplete" in failures
    assert "an ALL rule does not count" in failures
    monkeypatch.setattr(isolation, "sudoers_listing",
                        lambda settings: (_listing(settings), 0))
    assert isolation._outer_checks(settings) == []


# --- F8: swap off, IO weight, export size cap ---------------------------------------------


def test_unit_has_no_swap_and_a_lower_io_weight(tmp_path: Path) -> None:
    command = isolation.build_launch_command(_settings(tmp_path), UNIT)
    assert _property(command, "MemorySwapMax") == "0"
    assert _property(command, "IOWeight") == "50"
    command = isolation.build_launch_command(
        _settings(tmp_path, io_weight=None), UNIT)
    assert _property(command, "IOWeight") is None
    with pytest.raises(isolation.AgentIsolationError, match="io_weight"):
        _settings(tmp_path, io_weight=0)


def _scope(tmp_path: Path, monkeypatch, **files: str) -> str:
    monkeypatch.setattr(isolation, "CGROUP_ROOT", tmp_path / "cgroup")
    scope = tmp_path / "cgroup" / "app.slice" / f"{UNIT}.scope"
    scope.mkdir(parents=True, exist_ok=True)
    values = {"pids.max": "512", "memory.max": str(4 * 1024 ** 3),
              "memory.swap.max": "0", "cpu.weight": "100",
              "io.weight": "default 50\n8:0 200", "cgroup.kill": ""}
    values.update(files)
    for name, value in values.items():
        target = scope / name
        if value is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(f"{value}\n")
    return f"/app.slice/{UNIT}.scope"


def test_scope_with_swap_is_refused(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    isolation.verify_scope_cgroup(_scope(tmp_path, monkeypatch), settings)
    with pytest.raises(isolation.AgentIsolationError, match="memory.swap.max"):
        isolation.verify_scope_cgroup(
            _scope(tmp_path, monkeypatch, **{"memory.swap.max": "max"}),
            settings)


def test_scope_io_weight_is_verified_when_set(tmp_path: Path,
                                              monkeypatch) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(isolation.AgentIsolationError, match="io.weight"):
        isolation.verify_scope_cgroup(
            _scope(tmp_path, monkeypatch, **{"io.weight": "default 100"}),
            settings)
    with pytest.raises(isolation.AgentIsolationError,
                       match="io controller delegated"):
        isolation.verify_scope_cgroup(
            _scope(tmp_path, monkeypatch, **{"io.weight": None}), settings)
    isolation.verify_scope_cgroup(
        _scope(tmp_path, monkeypatch, **{"io.weight": None}),
        _settings(tmp_path, io_weight=None))


def test_handoff_tells_the_launcher_the_export_cap(repo: dict[str, Path],
                                                   tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_export_bytes=5 * 1024 ** 2)
    info = isolation.describe_worktree(str(repo["worktree"]))
    handoff = isolation.build_handoff(
        ["claude", "-p", "x"], str(repo["worktree"]),
        {"PATH": os.environ["PATH"]}, settings, UNIT, info, "tok",
        tmp_path / "handoff.bundle")
    assert handoff.header["workspace"]["max_export_bytes"] == 5 * 1024 ** 2


def test_launcher_never_publishes_an_export_over_the_cap(
        repo: dict[str, Path], tmp_path: Path, monkeypatch) -> None:
    import resource

    before = resource.getrlimit(resource.RLIMIT_FSIZE)
    settings = _settings(tmp_path)
    info = isolation.describe_worktree(str(repo["worktree"]))
    bundle = tmp_path / "handoff.bundle"
    handoff = isolation.build_handoff(
        ["claude", "-p", "x"], str(repo["worktree"]),
        {"PATH": os.environ["PATH"]}, settings, UNIT, info, "tok", bundle)
    handoff.header["workspace"]["max_export_bytes"] = 64 * 1024
    handoff.header["cgroup"]["path"] = f"/app.slice/{UNIT}.scope"
    session = agent_launcher._IsolatedSession(handoff.header)
    home = tmp_path / "agent-home"
    home.mkdir()
    session.home = str(home)
    with open(bundle, "rb") as source:
        session.receive_workspace(source.fileno(), bundle.stat().st_size)
    clone = Path(session.repo_dir)
    (clone / "blob.bin").write_bytes(os.urandom(512 * 1024))  # incompressible
    with pytest.raises(agent_launcher.IsolationRefused,
                       match="bundle failed|more than max_export_bytes"):
        session.export()
    exchange = Path(handoff.header["workspace"]["export_path"]).parent
    assert list(exchange.iterdir()) == []                # no export, no partial
    # Only the git child was limited, never this process.
    assert resource.getrlimit(resource.RLIMIT_FSIZE) == before
    session.discard()


def test_launcher_publishes_an_export_within_the_cap(
        repo: dict[str, Path], tmp_path: Path) -> None:
    def change(clone: Path) -> None:
        (clone / "small.txt").write_text("agent work\n")

    info, handoff, settings = _export_clone(repo, tmp_path, change)
    assert Path(handoff.export_path).is_file()
    assert handoff.header["workspace"]["max_export_bytes"] == \
        settings.max_export_bytes


@pytest.mark.parametrize("cap", [0, -1, True, "1024"])
def test_launcher_rejects_an_invalid_export_cap(tmp_path: Path, cap) -> None:
    workspace = {"handoff_ref": "refs/equipa/x", "branch_ref": "refs/heads/x",
                 "base_sha": "0" * 40, "export_path": "/x.bundle",
                 "carry_paths": [], "max_export_bytes": cap}
    with pytest.raises(agent_launcher.IsolationRefused, match="max_export"):
        agent_launcher._IsolatedSession(_launcher_header(tmp_path,
                                                         workspace=workspace))


# --- F9: the cgroup-path check and the scope-limit check at spawn are pinned --------------


def test_launcher_refuses_another_cgroup_even_with_the_right_limits(
        tmp_path: Path, monkeypatch) -> None:
    """M19: only the path is wrong; the limits all match, so nothing but
    the path check can refuse."""
    session = agent_launcher._IsolatedSession(_launcher_header(tmp_path))
    monkeypatch.setattr(agent_launcher, "_own_cgroup",
                        lambda: "/user.slice/session-1.scope")
    limits = {"pids.max": "64", "memory.max": str(256 * 1024 ** 2),
              "cpu.weight": "100"}
    monkeypatch.setattr(agent_launcher, "_read_cgroup_value",
                        lambda cgroup, name: limits[name])
    with pytest.raises(agent_launcher.IsolationRefused) as refused:
        session._verify_cgroup()
    assert str(refused.value) == (
        "running in cgroup '/user.slice/session-1.scope', expected "
        f"'/app.slice/{UNIT}.scope'")
    monkeypatch.setattr(agent_launcher, "_own_cgroup",
                        lambda: f"/app.slice/{UNIT}.scope")
    session._verify_cgroup()


class _Writer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _Reader:
    def __init__(self, line: bytes) -> None:
        self.line = line

    async def readline(self) -> bytes:
        return self.line

    async def read(self, size: int = -1) -> bytes:
        return b""


class _ReadyProcess:
    """A launcher stand-in that would report "ready" at once."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.returncode = None
        self.stdin = _Writer()
        self.stdout = _Reader(json.dumps(
            {"type": agent_launcher.HANDSHAKE_TYPE, "status": "ready"}
        ).encode() + b"\n")
        self.stderr = _Reader(b"")

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


def test_spawn_refuses_a_scope_whose_limits_are_wrong(
        tmp_path: Path, monkeypatch) -> None:
    """M22: the launcher would report ready, so only the orchestrator's own
    check of the scope's limits at spawn can refuse it."""
    lock_dir = tmp_path / "run"
    lock_dir.mkdir(mode=0o700)
    settings = _settings(tmp_path)
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    monkeypatch.setattr(isolation, "load_isolation_settings",
                        lambda config: settings)
    monkeypatch.setattr(isolation, "resolve_agent_identity", lambda s: None)
    monkeypatch.setattr(isolation, "check_host", lambda s, i: None)
    monkeypatch.setattr(isolation, "_unit_lock_dir", lambda: str(lock_dir))
    monkeypatch.setattr(isolation, "_SLOT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(isolation, "live_agent_scopes",
                        lambda app_slice=None: [])
    monkeypatch.setattr(isolation, "sweep_stale_scopes",
                        lambda app_slice=None: [])
    monkeypatch.setattr(isolation, "make_unit_name", lambda: UNIT)
    monkeypatch.setattr(isolation, "resolve_oauth_token", lambda s: "tok")
    monkeypatch.setattr(isolation, "build_handoff",
                        lambda *args: isolation.Handoff(
                            header={"cgroup": {"path": None}},
                            bundle_path=None, export_path=None))
    monkeypatch.setattr(isolation, "read_proc_cgroup",
                        lambda pid: f"/app.slice/{UNIT}.scope")
    # A registry of this test's own: should the spawn wrongly succeed, the
    # atexit cleanup must not wait on the fake scope and stall the run.
    monkeypatch.setattr(isolation, "_LIVE_ISOLATED_AGENTS", set())
    cgroup = _scope(tmp_path, monkeypatch, **{"pids.max": "max"})
    assert cgroup == f"/app.slice/{UNIT}.scope"
    process = _ReadyProcess()

    async def fake_exec(*argv, **kwargs):
        return process

    monkeypatch.setattr(isolation.asyncio, "create_subprocess_exec", fake_exec)

    async def spawn():
        return await isolation.spawn_isolated_agent(["claude"], None, {}, {})

    with pytest.raises(isolation.AgentIsolationError,
                       match="pids.max is 'max', expected 512"):
        asyncio.run(spawn())
    assert not process.stdin.data, "the handoff was sent to an unchecked scope"
    assert not isolation._LIVE_ISOLATED_AGENTS
