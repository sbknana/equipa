"""Task #3187 (IR83-01): a per-run ``--dispatch-config`` keeps the host
config's safety and layout settings.

A per-run file is merged over the defaults, not over the host config. Before
this task only agent isolation was carried, so a per-run file silently
dropped ``path_translations``: every project whose ``local_path`` was
recorded as a Windows drive path stopped resolving. The host's translations,
its scaffold source and its fail-closed gates now stay in a per-run config.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import equipa.config as config_mod
import equipa.tasks as tasks_mod
from equipa.config import (
    DEFAULT_DISPATCH_CONFIG,
    PATH_TRANSLATIONS_KEY,
    is_feature_enabled,
    load_dispatch_config,
    set_active_dispatch_config,
    translate_local_path,
)

SHARE = "X:\\share"
PROJECT_ID = 3187


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The host dispatch config (absent until a test writes it) and a share
    mount the host config maps ``X:\\share`` to."""
    state = SimpleNamespace(config=tmp_path / "host" / "dispatch_config.json",
                            mount=tmp_path / "share")
    state.config.parent.mkdir()
    state.mount.mkdir()
    monkeypatch.setattr(config_mod, "host_dispatch_config_path",
                        lambda: state.config)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_mod, "_warned_untranslated_paths", set(),
                        raising=False)
    set_active_dispatch_config({})
    yield state
    set_active_dispatch_config(None)


def _write_json(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _host_maps_the_share(host: SimpleNamespace, **extra: object) -> list[dict]:
    entries = [{"from": SHARE, "to": str(host.mount)}]
    _write_json(host.config, {PATH_TRANSLATIONS_KEY: entries, **extra})
    return entries


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


# --- path_translations --------------------------------------------------------


def test_a_per_run_config_keeps_the_host_path_translations(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    """The reviewer's reproduction: host translations, a per-run file that
    only scales the read budget."""
    entries = _host_maps_the_share(host)
    per_run = _write_json(tmp_path / "run-dispatch.json",
                          {"early_term_read_budget_scale": 2})

    config = load_dispatch_config(per_run)

    assert config["early_term_read_budget_scale"] == 2
    assert config[PATH_TRANSLATIONS_KEY] == entries
    assert translate_local_path(f"{SHARE}\\P", config) == f"{host.mount}/P"


def test_a_project_resolves_under_a_per_run_config(
    host: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end, as the CLI does it: load the per-run file, register it,
    resolve a project recorded with a drive path."""
    _host_maps_the_share(host)
    (host.mount / "Proj").mkdir()
    per_run = _write_json(tmp_path / "retry.json", {"max_retries": 2})
    monkeypatch.setattr(tasks_mod, "THEFORGE_DB", "theforge.db")
    monkeypatch.setattr(tasks_mod, "db_conn",
                        lambda *a, **k: _projects_db(f"{SHARE}\\Proj"))

    set_active_dispatch_config(load_dispatch_config(per_run))

    assert tasks_mod.resolve_project_dir({"project_id": PROJECT_ID}) == str(
        host.mount / "Proj")


def test_a_per_run_config_may_add_a_prefix_the_host_does_not_map(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    _host_maps_the_share(host)
    added = {"from": "W:\\other", "to": str(tmp_path / "other")}
    per_run = _write_json(tmp_path / "add.json", {PATH_TRANSLATIONS_KEY: [added]})

    config = load_dispatch_config(per_run)

    assert translate_local_path(f"{SHARE}\\P", config) == f"{host.mount}/P"
    assert translate_local_path("W:\\other\\P", config) == f"{tmp_path}/other/P"


@pytest.mark.parametrize("source", [
    SHARE,
    "X:/share/",            # same prefix, other separators
    "x:\\SHARE",            # a drive path is case-insensitive
    f"{SHARE}\\Proj",       # a subtree of the host prefix
])
def test_a_per_run_config_cannot_re_point_a_host_prefix(
    host: SimpleNamespace, tmp_path: Path, source: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The host decides where its share is mounted; the ``to`` prefixes are
    also the scaffold's mkdir allowlist."""
    _host_maps_the_share(host)
    elsewhere = tmp_path / "elsewhere"
    per_run = _write_json(tmp_path / "repoint.json", {PATH_TRANSLATIONS_KEY: [
        {"from": source, "to": str(elsewhere)}]})

    with caplog.at_level(logging.WARNING, logger="equipa.config"):
        config = load_dispatch_config(per_run)

    assert config[PATH_TRANSLATIONS_KEY] == [
        {"from": SHARE, "to": str(host.mount)}]
    assert translate_local_path(f"{SHARE}\\Proj\\x", config) == (
        f"{host.mount}/Proj/x")
    assert "cannot re-point it" in caplog.text


def test_a_per_run_copy_of_a_host_entry_is_kept_once_and_quietly(
    host: SimpleNamespace, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    entries = _host_maps_the_share(host)
    per_run = _write_json(tmp_path / "copy.json", {PATH_TRANSLATIONS_KEY: entries})

    with caplog.at_level(logging.WARNING, logger="equipa.config"):
        config = load_dispatch_config(per_run)

    assert config[PATH_TRANSLATIONS_KEY] == entries
    assert caplog.text == ""


@pytest.mark.parametrize("value", [None, {"from": SHARE, "to": "/x"}, "X:\\share"])
def test_a_per_run_value_that_is_not_a_list_keeps_the_host_entries(
    host: SimpleNamespace, tmp_path: Path, value: object,
) -> None:
    entries = _host_maps_the_share(host)
    per_run = _write_json(tmp_path / "odd.json", {PATH_TRANSLATIONS_KEY: value})

    assert load_dispatch_config(per_run)[PATH_TRANSLATIONS_KEY] == entries


def test_a_missing_per_run_file_keeps_the_host_path_translations(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    entries = _host_maps_the_share(host)

    config = load_dispatch_config(tmp_path / "mistyped.json")

    assert config[PATH_TRANSLATIONS_KEY] == entries


def test_loading_the_host_file_as_the_per_run_file_adds_nothing(
    host: SimpleNamespace,
) -> None:
    entries = _host_maps_the_share(host)

    assert load_dispatch_config(host.config)[PATH_TRANSLATIONS_KEY] == entries


def test_without_a_host_config_a_per_run_config_is_used_as_written(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    per_run = _write_json(tmp_path / "plain.json", {"max_retries": 2})

    config = load_dispatch_config(per_run)

    assert PATH_TRANSLATIONS_KEY not in config
    assert "forgescaffold_dir" not in config


def test_a_carried_entry_is_a_copy(host: SimpleNamespace, tmp_path: Path) -> None:
    _host_maps_the_share(host)
    per_run = _write_json(tmp_path / "x.json", {"max_retries": 1})

    first = load_dispatch_config(per_run)
    first[PATH_TRANSLATIONS_KEY][0]["to"] = "/changed"

    assert load_dispatch_config(per_run)[PATH_TRANSLATIONS_KEY][0]["to"] == str(
        host.mount)
    assert PATH_TRANSLATIONS_KEY not in DEFAULT_DISPATCH_CONFIG


# --- the other host-only settings ---------------------------------------------


def test_a_per_run_config_keeps_the_host_scaffold_source(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    _host_maps_the_share(host, forgescaffold_dir="/opt/forgescaffold")
    plain = _write_json(tmp_path / "plain.json", {"max_retries": 2})
    own = _write_json(tmp_path / "own.json",
                      {"forgescaffold_dir": "/opt/other-scaffold"})

    assert load_dispatch_config(plain)["forgescaffold_dir"] == "/opt/forgescaffold"
    assert load_dispatch_config(own)["forgescaffold_dir"] == "/opt/other-scaffold"


def test_a_per_run_config_cannot_turn_the_host_bash_hook_off(
    host: SimpleNamespace, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The PreToolUse hook is a fail-closed gate like agent isolation: a
    per-run config may turn it on, never off."""
    _write_json(host.config, {"features": {"bash_security_pretooluse": True}})
    plain = _write_json(tmp_path / "plain.json", {"max_retries": 2})
    off = _write_json(tmp_path / "off.json",
                      {"features": {"bash_security_pretooluse": False,
                                    "hooks": True}})

    assert is_feature_enabled(load_dispatch_config(plain),
                              "bash_security_pretooluse") is True
    with caplog.at_level(logging.WARNING, logger="equipa.config"):
        config = load_dispatch_config(off)
    assert is_feature_enabled(config, "bash_security_pretooluse") is True
    assert is_feature_enabled(config, "hooks") is True
    assert "cannot turn the Bash PreToolUse hook off" in caplog.text


def test_a_host_gate_that_is_off_stays_the_per_run_choice(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    _write_json(host.config, {"features": {"bash_security_pretooluse": False}})
    on = _write_json(tmp_path / "on.json",
                     {"features": {"bash_security_pretooluse": True}})
    plain = _write_json(tmp_path / "plain.json", {"max_retries": 2})

    assert is_feature_enabled(load_dispatch_config(on),
                              "bash_security_pretooluse") is True
    assert is_feature_enabled(load_dispatch_config(plain),
                              "bash_security_pretooluse") is False
    assert DEFAULT_DISPATCH_CONFIG["features"]["bash_security_pretooluse"] is False


# --- an untranslated drive path is reported ------------------------------------


def test_an_untranslated_drive_path_is_logged_once(
    host: SimpleNamespace, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="equipa.config"):
        assert translate_local_path("C:\\Proj", {}) == "C:\\Proj"
        assert translate_local_path("C:\\Proj", {}) == "C:\\Proj"
        assert translate_local_path("/srv/proj", {}) == "/srv/proj"
        assert translate_local_path("relative\\Proj", {}) == "relative\\Proj"
    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1
    assert "'C:\\\\Proj'" in warnings[0] and PATH_TRANSLATIONS_KEY in warnings[0]


@pytest.mark.parametrize("path, expected", [
    ("C:\\x", True), ("z:/x", True), ("C:", True),
    ("/srv/x", False), ("relative", False), ("", False), ("1:\\x", False),
    ("\N{LATIN CAPITAL LETTER A WITH GRAVE}:\\x", False),
])
def test_drive_letter_paths_are_recognised(path: str, expected: bool) -> None:
    assert config_mod.is_drive_letter_path(path) is expected
