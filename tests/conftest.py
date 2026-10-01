#!/usr/bin/env python3
"""Shared pytest fixtures for EQUIPA test suite.

Ensures database schema is created before any test runs by executing
schema.sql with CREATE TABLE IF NOT EXISTS semantics.

Copyright 2026 Forgeborn
"""

import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Add parent directory (repo root) to path for imports
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Also expose scripts/ so modules relocated there during repo cleanup (e.g.
# forgesmith_simba, forgesmith_impact) import by bare name in tests, exactly as
# forgesmith.py already arranges them at runtime. This makes `from
# forgesmith_simba import ...` resolve to scripts/forgesmith_simba.py on every
# platform, with no reliance on the repo-root symlink that Windows checkouts
# (core.symlinks=false) materialize as a broken text file.
_SCRIPTS_DIR = REPO_ROOT / "scripts"
if _SCRIPTS_DIR.is_dir():
    sys.path.insert(0, str(_SCRIPTS_DIR))

import pytest  # noqa: E402  (sys.path must be set before any equipa import)

# --- Test-DB isolation (EQUIPA review 2026-09-29: LRN-01 / mcpdb-04 / claims-06) ---
#
# On the owner's hosts the repo-root theforge.db is a SYMLINK to the live
# TheForge database, and this suite runs DELETEs and schema changes against
# whatever THEFORGE_DB resolves to. Running pytest in the source checkout
# therefore wiped the live agent_episodes / lessons_learned tables, repeatedly.
#
# So before ANY equipa module is imported, THEFORGE_DB is pointed at a fresh
# throwaway file. The ambient value is overridden ON PURPOSE: no test needs a
# real TheForge DB, and an inherited production path is exactly how the
# damage happened. _assert_db_isolated() then checks every loaded module's
# binding and stops the run before any test executes if one escaped.
_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="equipa-test-db-"))
TEST_DB_PATH = _TEST_DB_DIR / "theforge-test.db"
os.environ["THEFORGE_DB"] = str(TEST_DB_PATH)


def _is_safe_test_db(path) -> bool:
    """True only for a DB path that cannot be a real TheForge database.

    The path must not itself be a symlink, and after resolving any symlinks
    in its parents it must sit inside the system temp directory (where this
    conftest's DB and every tmp_path live). A repo-root theforge.db that is a
    symlink to production fails both tests. Nothing inside the repo counts
    as safe either, even when the checkout itself lives under /tmp: the
    repo-root default is never a test DB.
    """
    p = Path(path)
    if p.is_symlink():
        return False
    tmp_root = Path(tempfile.gettempdir()).resolve()
    try:
        resolved = p.resolve()
    except OSError:
        return False
    if resolved.is_relative_to(REPO_ROOT):
        return False
    return resolved.is_relative_to(tmp_root)


def _db_bindings():
    """Yield (module name, value) for every THEFORGE_DB binding in loaded
    project modules (equipa.* plus repo-root scripts), excluding tests.

    Several modules import THEFORGE_DB by value at import time, so checking
    equipa.constants alone would miss a stale copy (see mcpdb-03)."""
    for name, mod in list(sys.modules.items()):
        if mod is None or not _is_project_module(name, mod):
            continue
        value = getattr(mod, "THEFORGE_DB", None)
        if value is not None:
            yield name, value


# Module name -> "lives in this repo, outside tests/". Resolving every loaded
# module's path around each of ~3,000 tests is slow, so it is decided once.
_PROJECT_MODULE_CACHE: dict = {}


def _is_project_module(name, mod) -> bool:
    cached = _PROJECT_MODULE_CACHE.get(name)
    if cached is not None and cached[0] is mod:
        return cached[1]
    result = False
    mod_file = getattr(mod, "__file__", None)
    if mod_file:
        try:
            mod_path = Path(mod_file).resolve()
            result = (mod_path.is_relative_to(REPO_ROOT)
                      and not mod_path.is_relative_to(REPO_ROOT / "tests"))
        except OSError:
            result = False
    _PROJECT_MODULE_CACHE[name] = (mod, result)
    return result


def _assert_db_isolated(stage: str) -> None:
    """Abort the whole run if any loaded module points outside the temp dir."""
    escaped = [f"{name}.THEFORGE_DB = {value}"
               for name, value in _db_bindings()
               if not _is_safe_test_db(value)]
    if escaped:
        pytest.exit(
            f"[conftest] REFUSING TO RUN ({stage}): a TheForge DB path escaped "
            "the test sandbox and could point at a real database:\n  "
            + "\n  ".join(escaped),
            returncode=3,
        )



@pytest.fixture(autouse=True)
def _restore_db_bindings():
    """Undo any THEFORGE_DB swap a test makes, whether or not it cleans up.

    Several tests point equipa.constants / equipa.db (and friends) at their
    own temp DB by hand; some never restore it, which leaked an empty DB into
    every later test and made results depend on test order. Snapshot every
    binding (and the env var) before each test and put them back after.
    """
    saved = [(sys.modules[name], value) for name, value in _db_bindings()]
    saved_env = os.environ.get("THEFORGE_DB")
    yield
    for module, value in saved:
        module.THEFORGE_DB = value
    if saved_env is None:
        os.environ.pop("THEFORGE_DB", None)
    else:
        os.environ["THEFORGE_DB"] = saved_env


@pytest.fixture(scope="session")
def _absent_host_path(tmp_path_factory) -> Path:
    """A directory that does not exist, for host-state paths in tests."""
    return tmp_path_factory.mktemp("no-host-isolation") / "absent"


@pytest.fixture(autouse=True)
def _no_host_agent_isolation_state(_absent_host_path, monkeypatch):
    """Task 3142 (review F1): the host's isolation marker and dispatch
    config make agent isolation sticky. On a host where the operator has
    enabled it, every test would otherwise see isolation required; tests
    of that state set both paths themselves."""
    from equipa import config as equipa_config
    from equipa import isolation as equipa_isolation

    absent = _absent_host_path
    monkeypatch.setattr(equipa_isolation, "REQUIRED_MARKER", absent / "marker")
    monkeypatch.setattr(equipa_config, "host_dispatch_config_path",
                        lambda: absent / "dispatch_config.json")
    yield


@pytest.fixture(autouse=True)
def _isolate_reviewer_run_registry():
    """Task #3041: reviewer-run provenance is process-global; a run recorded
    by one test must never decide another test's gate for the same task id.

    Task #3063 (SR41-03): production BLOCKS a gate evaluation that has no
    reviewer record. The many hermetic gate tests that exercise count parsing
    on a hand-written artifact, without running a reviewer, opt in to the
    pre-#3041 artifact-only trust here — explicitly, and only for tests.
    Tests of the no-record block itself turn it back off (see
    tests/test_gate_provenance_3063.py)."""
    from equipa.security_gate import (
        clear_reviewer_runs,
        set_unrecorded_reviewer_runs_permitted,
    )

    clear_reviewer_runs()
    previous = set_unrecorded_reviewer_runs_permitted(True)
    yield
    set_unrecorded_reviewer_runs_permitted(previous)
    clear_reviewer_runs()


def _make_idempotent(schema_sql: str) -> str:
    """Rewrite CREATE statements to be idempotent (IF NOT EXISTS).

    Covers TABLE / VIEW / TRIGGER / INDEX / UNIQUE INDEX so applying a schema
    file more than once (or over an already-populated DB) is a safe no-op.
    """
    for keyword in ("TABLE", "VIEW", "TRIGGER", "INDEX"):
        schema_sql = re.sub(
            rf"CREATE {keyword}(?!\s+IF\s+NOT\s+EXISTS)",
            f"CREATE {keyword} IF NOT EXISTS",
            schema_sql,
            flags=re.IGNORECASE,
        )
    schema_sql = re.sub(
        r"CREATE UNIQUE INDEX(?!\s+IF\s+NOT\s+EXISTS)",
        "CREATE UNIQUE INDEX IF NOT EXISTS",
        schema_sql,
        flags=re.IGNORECASE,
    )
    return schema_sql


def _apply_schema_file(conn, schema_path):
    """Idempotently apply one schema .sql file to an open connection."""
    if not schema_path.exists():
        print(f"  [conftest] WARNING: schema not found at {schema_path}")
        return
    try:
        conn.executescript(_make_idempotent(schema_path.read_text()))
        conn.commit()
    except Exception as e:
        print(f"  [conftest] WARNING: {schema_path.name} partial apply: {e}")


def _ensure_full_schema():
    """Apply BOTH schema files to the test database, creating missing tables.

    The test suite needs every table — including the owner-only personal-PM
    tables split into schema_personal.sql (task 2707) — so both files are
    applied here regardless of the personal_pm_tables feature flag. Both are
    rewritten to CREATE ... IF NOT EXISTS, so re-applying is a safe no-op.
    """
    from equipa.constants import THEFORGE_DB

    conn = sqlite3.connect(THEFORGE_DB)
    try:
        _apply_schema_file(conn, REPO_ROOT / "schema.sql")
        _apply_schema_file(conn, REPO_ROOT / "schema_personal.sql")
    finally:
        conn.close()


def pytest_configure(config):
    """Ensure database schema exists before any tests collect."""
    from equipa import constants as equipa_constants

    if Path(equipa_constants.THEFORGE_DB) != TEST_DB_PATH:
        pytest.exit(
            "[conftest] REFUSING TO RUN: equipa.constants was imported before "
            f"the test DB was set (THEFORGE_DB={equipa_constants.THEFORGE_DB}).",
            returncode=3,
        )
    _assert_db_isolated("configure")
    try:
        from equipa import db as equipa_db

        # Reset ensure_schema cache so it re-runs for test DB
        equipa_db._SCHEMA_ENSURED = False
        equipa_db.ensure_schema()

        # Apply the full schema.sql for tables not covered by ensure_schema()
        _ensure_full_schema()
    except (ImportError, ModuleNotFoundError) as e:
        print(f"  [conftest] WARNING: could not import equipa.db: {e}")
        print("  [conftest] Schema setup skipped — tests requiring DB may fail.")


def pytest_collection_modifyitems(session, config, items):
    """After collection, call setup_test_data() for modules that define it.

    This replaces the manual setup that was done in each module's run_all_tests().
    Every test module is imported by now, so the isolation check runs first:
    setup_test_data() itself writes to the DB.
    """
    _assert_db_isolated("collection")
    setup_modules = set()
    for item in items:
        module = item.module
        if module not in setup_modules and hasattr(module, "setup_test_data"):
            try:
                module.setup_test_data()
            except Exception as e:
                print(f"  [conftest] WARNING: setup_test_data() failed for {module.__name__}: {e}")
            setup_modules.add(module)


def pytest_unconfigure(config):
    """Remove this run's throwaway DB directory."""
    shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)
