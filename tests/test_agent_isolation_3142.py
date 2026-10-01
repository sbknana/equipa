"""Task 3142 (ISO-4): pre-enable fixes for agent isolation.

Copyright 2026 Forgeborn

Findings of the independent isolation review (F1-F10, I1, I5) and
SECURITY-REVIEW-3140 (R3140-01). Each test fails on main before the fix:

* F3 / R3140-01: the import's link check is linear in the target length
  and bounded (target size, links per tree, components walked).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from equipa import isolation

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
