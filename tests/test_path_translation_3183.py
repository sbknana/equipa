"""Task #3183 (IR80-05): a project ``local_path`` recorded on another host is
mapped by the dispatch config's ``path_translations``, never by a built-in
operator path. One helper (``equipa.config.translate_local_path``) serves
both places that turn a DB ``local_path`` into a directory; the scaffold
bootstrap keeps its containment check after translation.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import logging
import re
import sqlite3
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

import equipa.db as db_mod
import equipa.dispatch as dispatch_mod
import equipa.scaffold as scaffold
import equipa.tasks as tasks_mod
from equipa.config import (
    DEFAULT_DISPATCH_CONFIG,
    PATH_TRANSLATIONS_KEY,
    configured_path_translations,
    set_active_dispatch_config,
    translate_local_path,
)

SHARE = "X:\\share"
MOUNT = "/srv/share"
SHARE_TO_MOUNT = {PATH_TRANSLATIONS_KEY: [{"from": SHARE, "to": MOUNT}]}
PROJECT_ID = 3183


@pytest.fixture(autouse=True)
def _registered_config_is_reset() -> Iterator[None]:
    """Each test registers the config it means; none leaks into the next."""
    set_active_dispatch_config({})
    yield
    set_active_dispatch_config(None)


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


def _share_mounted_at(tmp_path: Path) -> Path:
    """Register ``X:\\share`` -> ``<tmp>/share`` and return the mount."""
    mount = tmp_path / "share"
    mount.mkdir()
    set_active_dispatch_config(
        {PATH_TRANSLATIONS_KEY: [{"from": SHARE, "to": str(mount)}]})
    return mount


# --- the helper ---------------------------------------------------------------


@pytest.mark.parametrize("config", [{}, dict(DEFAULT_DISPATCH_CONFIG),
                                    {PATH_TRANSLATIONS_KEY: []},
                                    {PATH_TRANSLATIONS_KEY: None}])
def test_no_path_is_translated_by_default(config: dict) -> None:
    """There is no built-in mapping: a path is used as recorded."""
    for recorded in (f"{SHARE}\\Proj", "Z:\\Projects\\App", "/srv/share/Proj"):
        assert translate_local_path(recorded, config) == recorded
    assert configured_path_translations(config) == []


@pytest.mark.parametrize("recorded, translated", [
    (f"{SHARE}\\Proj", f"{MOUNT}/Proj"),
    ("X:/share/Proj", f"{MOUNT}/Proj"),
    (f"{SHARE}/Proj\\sub dir", f"{MOUNT}/Proj/sub dir"),
    (SHARE, MOUNT),
    (f"{SHARE}\\", f"{MOUNT}/"),
    # A whole segment only, case-sensitively, from the start only.
    ("X:\\shared\\Proj", "X:\\shared\\Proj"),
    ("x:\\share\\Proj", "x:\\share\\Proj"),
    ("Y:\\share\\Proj", "Y:\\share\\Proj"),
    ("C:\\X:\\share\\Proj", "C:\\X:\\share\\Proj"),
    (f"{MOUNT}/Proj", f"{MOUNT}/Proj"),
    ("", ""),
])
def test_a_configured_prefix_is_mapped(recorded: str, translated: str) -> None:
    assert translate_local_path(recorded, SHARE_TO_MOUNT) == translated


def test_the_longest_matching_prefix_wins_whatever_the_order() -> None:
    config = {PATH_TRANSLATIONS_KEY: [
        {"from": SHARE, "to": MOUNT},
        {"from": f"{SHARE}\\big", "to": "/mnt/big"},
    ]}
    assert translate_local_path(f"{SHARE}\\big\\Proj", config) == "/mnt/big/Proj"
    assert translate_local_path(f"{SHARE}\\bigger", config) == f"{MOUNT}/bigger"
    assert translate_local_path(f"{SHARE}\\other", config) == f"{MOUNT}/other"


def test_trailing_separators_of_an_entry_do_not_matter() -> None:
    config = {PATH_TRANSLATIONS_KEY: [{"from": f"{SHARE}\\", "to": f"{MOUNT}/"}]}
    assert translate_local_path(f"{SHARE}\\Proj", config) == f"{MOUNT}/Proj"


def test_a_root_target_keeps_an_absolute_path() -> None:
    config = {PATH_TRANSLATIONS_KEY: [{"from": SHARE, "to": "/"}]}
    assert translate_local_path(f"{SHARE}\\Proj", config) == "/Proj"
    assert translate_local_path(SHARE, config) == "/"


@pytest.mark.parametrize("entry", [
    "X:\\share=/srv/share",                      # not an object
    {"from": SHARE},                             # no target
    {"to": MOUNT},                               # no source
    {"from": SHARE, "to": 7},                    # not a string
    {"from": SHARE, "to": "srv/share"},          # relative target
    {"from": "", "to": MOUNT},                   # matches every path
    {"from": "\\", "to": MOUNT},                 # a bare root
    {"from": "/", "to": MOUNT},
    {"from": "X:\\share\\..", "to": MOUNT},      # a parent segment
    {"from": SHARE, "to": "/srv/share/../etc"},
])
def test_an_unusable_entry_is_logged_and_ignored(
    entry: object, caplog: pytest.LogCaptureFixture,
) -> None:
    config = {PATH_TRANSLATIONS_KEY: [entry, {"from": "W:\\other", "to": "/mnt/other"}]}
    with caplog.at_level(logging.ERROR, logger="equipa.config"):
        assert translate_local_path(f"{SHARE}\\Proj", config) == f"{SHARE}\\Proj"
        # The usable entry beside it still applies.
        assert translate_local_path("W:\\other\\Proj", config) == "/mnt/other/Proj"
    assert any(PATH_TRANSLATIONS_KEY in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("value", [{"from": SHARE, "to": MOUNT}, SHARE, 1])
def test_a_value_that_is_not_a_list_translates_nothing(
    value: object, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="equipa.config"):
        result = translate_local_path(f"{SHARE}\\Proj", {PATH_TRANSLATIONS_KEY: value})
    assert result == f"{SHARE}\\Proj"
    assert any("must be a list" in record.getMessage() for record in caplog.records)


def test_a_path_that_is_not_a_string_is_refused() -> None:
    with pytest.raises(TypeError, match="local_path must be a str"):
        translate_local_path(Path("/srv/share"), SHARE_TO_MOUNT)  # type: ignore[arg-type]


def test_the_registered_dispatch_config_is_read_when_none_is_passed() -> None:
    set_active_dispatch_config(SHARE_TO_MOUNT)
    assert translate_local_path(f"{SHARE}\\Proj") == f"{MOUNT}/Proj"
    set_active_dispatch_config({})
    assert translate_local_path(f"{SHARE}\\Proj") == f"{SHARE}\\Proj"


# --- call site 1: tasks.resolve_project_dir -----------------------------------------


def _resolve_from_db(monkeypatch: pytest.MonkeyPatch, local_path: str) -> str | None:
    monkeypatch.setattr(tasks_mod, "THEFORGE_DB", "theforge.db")
    monkeypatch.setattr(tasks_mod, "db_conn", lambda *a, **k: _projects_db(local_path))
    return tasks_mod.resolve_project_dir({"project_id": PROJECT_ID})


def test_resolve_project_dir_maps_a_recorded_share_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount = _share_mounted_at(tmp_path)
    (mount / "Proj" / "sub").mkdir(parents=True)

    assert _resolve_from_db(monkeypatch, f"{SHARE}\\Proj\\sub\\") == str(mount / "Proj" / "sub")


def test_resolve_project_dir_uses_an_untranslated_path_as_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "Proj").mkdir()
    assert _resolve_from_db(monkeypatch, f"{SHARE}\\Proj") is None
    assert _resolve_from_db(monkeypatch, f"{tmp_path}/Proj/") == str(tmp_path / "Proj")


# --- call site 2: dispatch._bootstrap_scaffold_if_needed ------------------------------


def _bootstrap(monkeypatch: pytest.MonkeyPatch, local_path: str) -> str | None:
    monkeypatch.setattr(scaffold, "is_scaffold_project", lambda project_id: True)
    monkeypatch.setattr(db_mod, "db_conn", lambda *a, **k: _projects_db(local_path))
    return dispatch_mod._bootstrap_scaffold_if_needed({}, PROJECT_ID)


def test_the_scaffold_bootstrap_maps_a_recorded_share_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The translated path lands under the mount, which is also the
    allowlisted root when EQUIPA_SCAFFOLD_ALLOWED_ROOTS is unset."""
    monkeypatch.delenv("EQUIPA_SCAFFOLD_ALLOWED_ROOTS", raising=False)
    mount = _share_mounted_at(tmp_path)

    created = _bootstrap(monkeypatch, f"{SHARE}\\NewProj")

    assert created == str((mount / "NewProj").resolve())
    assert (mount / "NewProj").is_dir()


@pytest.mark.parametrize("recorded", [
    f"{SHARE}\\..\\..\\evil",
    f"{SHARE}/NewProj/../../evil",
])
def test_the_scaffold_bootstrap_still_refuses_a_path_that_escapes_the_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded: str,
) -> None:
    monkeypatch.delenv("EQUIPA_SCAFFOLD_ALLOWED_ROOTS", raising=False)
    _share_mounted_at(tmp_path)

    assert _bootstrap(monkeypatch, recorded) is None
    assert not (tmp_path / "evil").exists()
    assert not (tmp_path.parent / "evil").exists()


def test_the_scaffold_bootstrap_refuses_a_path_outside_every_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EQUIPA_SCAFFOLD_ALLOWED_ROOTS", raising=False)
    _share_mounted_at(tmp_path)
    outside = tmp_path / "outside" / "NewProj"

    assert _bootstrap(monkeypatch, str(outside)) is None
    assert not outside.exists()


def test_both_call_sites_translate_through_the_one_helper() -> None:
    """No call site keeps its own prefix check: each calls
    translate_local_path and holds no drive-letter path literal."""
    drive_path = re.compile(r"^[A-Za-z]:[\\/]")
    for function in (tasks_mod.resolve_project_dir,
                     dispatch_mod._bootstrap_scaffold_if_needed):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        literals = [node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and drive_path.match(node.value)]
        assert "translate_local_path" in called, function.__name__
        assert literals == [], (function.__name__, literals)


# --- scaffold: no built-in operator location ------------------------------------------


def test_with_no_root_configured_every_scaffold_clone_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EQUIPA_SCAFFOLD_ALLOWED_ROOTS", raising=False)

    with pytest.raises(scaffold.ScaffoldCloneError, match="no allowlisted root"):
        scaffold.assert_contained_path(str(tmp_path / "NewProj"))


def test_the_scaffold_roots_are_the_translation_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EQUIPA_SCAFFOLD_ALLOWED_ROOTS", raising=False)
    mount = _share_mounted_at(tmp_path)

    assert scaffold.assert_contained_path(str(mount / "P")) == (mount / "P").resolve()
    with pytest.raises(scaffold.ScaffoldCloneError, match="not inside"):
        scaffold.assert_contained_path(str(tmp_path / "elsewhere" / "P"))


def test_the_scaffold_roots_variable_wins_over_the_translation_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount = _share_mounted_at(tmp_path)
    allowed = tmp_path / "allowed"
    monkeypatch.setenv("EQUIPA_SCAFFOLD_ALLOWED_ROOTS", str(allowed))

    assert scaffold.assert_contained_path(str(allowed / "P")) == (allowed / "P").resolve()
    with pytest.raises(scaffold.ScaffoldCloneError, match="not inside"):
        scaffold.assert_contained_path(str(mount / "P"))


def test_the_scaffold_source_has_no_built_in_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EQUIPA_FORGESCAFFOLD_DIR", raising=False)

    assert scaffold.resolve_scaffold_source() is None
    assert scaffold.resolve_scaffold_source({"forgescaffold_dir": ""}) is None


def test_an_unconfigured_scaffold_source_refuses_the_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EQUIPA_FORGESCAFFOLD_DIR", raising=False)
    monkeypatch.setenv("EQUIPA_SCAFFOLD_ALLOWED_ROOTS", str(tmp_path))
    destination = tmp_path / "NewProj"

    with pytest.raises(scaffold.ScaffoldCloneError, match="source is not configured"):
        scaffold.ensure_scaffold(destination, PROJECT_ID, force=True)
    assert not destination.exists()
