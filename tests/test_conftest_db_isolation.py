#!/usr/bin/env python3
"""The conftest test-DB guard must reject anything that could be a real DB.

Known-bad controls for tests/conftest.py::_is_safe_test_db and the binding
sweep, so the isolation check can't silently pass (EQUIPA review 2026-09-29,
LRN-01 / claims-06).

Copyright 2026 Forgeborn
"""

import os
import sys
import types
from pathlib import Path

import pytest

import conftest


def test_suite_db_env_points_at_the_conftest_temp_db():
    assert os.environ["THEFORGE_DB"] == str(conftest.TEST_DB_PATH)
    assert conftest._is_safe_test_db(conftest.TEST_DB_PATH)


def test_path_outside_temp_is_rejected():
    assert not conftest._is_safe_test_db(Path.home() / "theforge.db")
    assert not conftest._is_safe_test_db(conftest.REPO_ROOT / "theforge.db")


def test_symlink_is_rejected_even_inside_temp(tmp_path):
    target = tmp_path / "real.db"
    target.write_bytes(b"")
    link = tmp_path / "theforge.db"
    link.symlink_to(target)
    assert conftest._is_safe_test_db(target)
    assert not conftest._is_safe_test_db(link)


def test_symlink_escaping_temp_is_rejected(tmp_path):
    outside = conftest.REPO_ROOT / "README.md"
    link = tmp_path / "theforge.db"
    link.symlink_to(outside)
    assert not conftest._is_safe_test_db(link)


def test_sweep_catches_a_stale_module_binding(monkeypatch):
    """A repo module holding a production-looking path must stop the run."""
    fake = types.ModuleType("equipa._isolation_probe")
    fake.__file__ = str(conftest.REPO_ROOT / "equipa" / "_isolation_probe.py")
    fake.THEFORGE_DB = conftest.REPO_ROOT / "theforge.db"
    monkeypatch.setitem(sys.modules, "equipa._isolation_probe", fake)

    with pytest.raises(pytest.exit.Exception) as exc:
        conftest._assert_db_isolated("unit-test")
    assert "equipa._isolation_probe.THEFORGE_DB" in str(exc.value)
    assert exc.value.returncode == 3


def test_sweep_passes_for_the_real_loaded_modules():
    conftest._assert_db_isolated("unit-test")  # raises pytest.exit on failure
    names = [name for name, _ in conftest._db_bindings()]
    assert "equipa.constants" in names, names
