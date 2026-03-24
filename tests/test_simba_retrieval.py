"""Test: get_active_simba_rules returns active rules from DB.

Copyright 2026 Forgeborn
"""
from __future__ import annotations

import sqlite3
import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _stub_equipa_deps():
    """Stub missing equipa sub-packages so lessons.py can be imported."""
    # equipa.db
    db_mod = types.ModuleType("equipa.db")
    db_mod.ensure_schema = MagicMock()
    db_mod.get_db_connection = MagicMock()

    # equipa.constants
    constants_mod = types.ModuleType("equipa.constants")
    constants_mod.THEFORGE_DB = ":memory:"

    # equipa.parsing — use MagicMock so any attribute access succeeds
    parsing_mod = MagicMock()
    parsing_mod.__name__ = "equipa.parsing"

    saved = {}
    for name, mod in [
        ("equipa.db", db_mod),
        ("equipa.constants", constants_mod),
        ("equipa.parsing", parsing_mod),
    ]:
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod

    yield

    # Restore original state
    for name, original in saved.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original

    # Force reimport on next test collection
    sys.modules.pop("equipa.lessons", None)


def test_get_active_simba_rules(tmp_path, monkeypatch):
    """get_active_simba_rules returns active simba/forgesmith rules, excluding inactive."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE lessons_learned ("
        "id INTEGER PRIMARY KEY, project_id INT, role TEXT, "
        "error_type TEXT, error_signature TEXT, lesson TEXT, "
        "source TEXT, times_seen INT DEFAULT 1, "
        "times_injected INT DEFAULT 0, effectiveness_score REAL, "
        "active INT DEFAULT 1, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO lessons_learned (lesson, source, active, error_signature) "
        "VALUES ('Always run linter', 'simba', 1, 'lint_rule')"
    )
    conn.execute(
        "INSERT INTO lessons_learned (lesson, source, active, error_signature) "
        "VALUES ('Check types', 'forgesmith', 1, 'type_rule')"
    )
    conn.execute(
        "INSERT INTO lessons_learned (lesson, source, active, error_signature) "
        "VALUES ('Old rule', 'simba', 0, 'old')"
    )
    conn.commit()
    conn.close()

    def _mock_db_connection(write: bool = False) -> sqlite3.Connection:
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(
        "equipa.lessons.get_db_connection", _mock_db_connection
    )

    from equipa.lessons import get_active_simba_rules

    rules = get_active_simba_rules()

    # Both active simba and forgesmith rules returned
    assert len(rules) == 2
    assert any("linter" in r["lesson"] for r in rules)
    assert any("types" in r["lesson"] for r in rules)
    # Inactive rule excluded
    assert not any("Old rule" in r["lesson"] for r in rules)
    # Each rule has expected keys
    for rule in rules:
        assert "lesson" in rule
        assert "signature" in rule
