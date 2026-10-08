"""Task #3187 (IR83-02): the scaffold containment check refuses a path that
is not absolute on this host.

``assert_contained_path`` resolved a relative path against the process cwd.
With an allowlisted root holding the cwd, ``C:\\x``, ``X:\\share\\Proj``
(a Windows path no translation maps) and ``relative/p`` were allowed and
became junk directories such as ``<cwd>/C:\\x``. Only an absolute path of
this host may now be created.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

import equipa.db as db_mod
import equipa.dispatch as dispatch_mod
import equipa.scaffold as scaffold
from equipa.config import set_active_dispatch_config

PROJECT_ID = 3187


def test_these_cases_run_on_a_posix_host() -> None:
    """The orchestrator host (and CI) is POSIX, where a drive or backslash
    form is a relative file name; on Windows it would be a real path."""
    assert os.name == "posix"


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The reviewer's layout: the allowlisted root holds the process cwd."""
    root = tmp_path / "root"
    cwd = root / "checkout"
    cwd.mkdir(parents=True)
    monkeypatch.setenv("EQUIPA_SCAFFOLD_ALLOWED_ROOTS", str(root))
    monkeypatch.chdir(cwd)
    set_active_dispatch_config({})
    yield cwd
    set_active_dispatch_config(None)


@pytest.mark.parametrize("candidate", [
    "relative/p",
    "p",
    "./p",
])
def test_a_relative_path_is_refused(checkout: Path, candidate: str) -> None:
    with pytest.raises(scaffold.ScaffoldCloneError,
                       match="not an absolute path on this host"):
        scaffold.assert_contained_path(candidate)


@pytest.mark.parametrize("candidate", [
    "C:\\x",
    "C:x",
    "X:\\share\\Proj",
    "X:/share/Proj",
    "H:\\",
])
def test_an_untranslated_windows_path_is_refused(
    checkout: Path, candidate: str,
) -> None:
    with pytest.raises(scaffold.ScaffoldCloneError,
                       match="no path_translations entry maps"):
        scaffold.assert_contained_path(candidate)


def test_an_absolute_path_with_a_backslash_is_refused(checkout: Path) -> None:
    with pytest.raises(scaffold.ScaffoldCloneError, match="not an absolute"):
        scaffold.assert_contained_path(f"{checkout}/Proj\\sub")


def test_an_absolute_path_inside_the_root_is_still_allowed(
    checkout: Path,
) -> None:
    wanted = checkout.parent / "NewProj"
    assert scaffold.assert_contained_path(str(wanted)) == wanted.resolve()
    assert scaffold.assert_contained_path(wanted) == wanted.resolve()


@contextlib.contextmanager
def _projects_db(local_path: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, local_path TEXT)")
    conn.execute("INSERT INTO projects (id, local_path) VALUES (?, ?)",
                 (PROJECT_ID, local_path))
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize("recorded", ["C:\\x", "X:\\share\\Proj", "relative/p"])
def test_the_scaffold_bootstrap_creates_nothing_in_the_checkout(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, recorded: str,
) -> None:
    """End to end: a recorded path no translation maps creates no
    directory under the cwd."""
    monkeypatch.setattr(scaffold, "is_scaffold_project", lambda project_id: True)
    monkeypatch.setattr(db_mod, "db_conn", lambda *a, **k: _projects_db(recorded))

    assert dispatch_mod._bootstrap_scaffold_if_needed({}, PROJECT_ID) is None
    assert list(checkout.iterdir()) == []
