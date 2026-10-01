"""Task 3142 (ISO-4): pre-enable fixes for agent isolation.

Copyright 2026 Forgeborn

Findings of the independent isolation review (F1-F10, I1, I5) and
SECURITY-REVIEW-3140 (R3140-01). Each test fails on main before the fix:

* F3 / R3140-01: the import's link check is linear in the target length
  and bounded (target size, links per tree, components walked).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
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
        host, tmp_path: Path, capsys) -> None:
    from equipa.config import is_feature_enabled, load_dispatch_config

    _write_json(host.config, {"features": {"agent_isolation": True},
                              "agent_isolation": _SECTION})
    config = load_dispatch_config(tmp_path / "mistyped.json")
    assert is_feature_enabled(config, "agent_isolation") is True
    assert config["agent_isolation"] == _SECTION
    assert "does not exist" in capsys.readouterr().out


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
