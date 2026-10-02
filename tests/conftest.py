#!/usr/bin/env python3
"""Shared pytest fixtures for EQUIPA test suite.

Ensures database schema is created before any test runs by executing
schema.sql with CREATE TABLE IF NOT EXISTS semantics.

Copyright 2026 Forgeborn
"""

import os
import re
import shutil
import signal
import sqlite3
import stat
import sys
import tempfile
import time
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
#
# --- Per-session temp directory (R3150-08) ---
#
# Every Claude CLI run gets a per-run CLAUDE_CONFIG_DIR (equipa-claude-config-*)
# in the temp directory, and the orchestrator scripts some tests SIGKILL never
# remove theirs. So everything this session puts in the temp directory, in
# this process and in every subprocess (they inherit TMPDIR), goes into one
# private directory that pytest_unconfigure removes. Per-run config
# directories are never swept from the shared temp directory instead: agents
# run this suite while the orchestrator dispatches, and a live run's directory
# looks the same as a leaked one. Set before the test DB below, so it lives
# inside it too and _is_safe_test_db() judges against the same temp root.
#
# pytest_unconfigure never runs when the session is killed. The tester runs
# the suite under `timeout`, whose SIGTERM is therefore handled below and
# removes the session directory first. A SIGKILLed session's directory is
# removed by the next session once it is a day old (only "eqt-" directories
# of this user: no session lasts that long, so a live one is never taken).
_ORIGINAL_TMP = tempfile.gettempdir()
_SESSION_TMP_PREFIX = "eqt-"
STALE_SESSION_TMP_SECONDS = 24 * 3600


def _sweep_stale_session_dirs(root: str,
                              max_age_seconds: float = STALE_SESSION_TMP_SECONDS,
                              now: float | None = None) -> list[str]:
    """Remove the session temp directories killed sessions left in *root*.

    Only real directories (never symlinks) named ``eqt-*``, owned by this
    user, whose own mtime and every direct entry's mtime are older than
    *max_age_seconds*. Returns the paths that were removed.
    """
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed: list[str] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return removed
    for entry in entries:
        if not entry.name.startswith(_SESSION_TMP_PREFIX):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            continue
        newest = info.st_mtime
        try:
            with os.scandir(entry.path) as children:
                for child in children:
                    newest = max(newest,
                                 child.stat(follow_symlinks=False).st_mtime)
        except OSError:
            continue
        if newest > cutoff:
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        if not os.path.lexists(entry.path):
            removed.append(entry.path)
    return removed


#
# --- pytest-xdist workers (task 3160) ---
#
# Under `pytest -n N` every worker is its own process and runs this file
# itself, so each worker gets its own session directory (inside the
# controller's: the worker inherits TMPDIR) and its own THEFORGE_DB. Workers
# never share a database. tmp_path is different: the controller hands every
# worker a base directory under ITS basetemp (<controller session>/
# pytest-of-<user>/pytest-N/popen-gwN), outside the worker's own session
# directory. So a worker judges test-DB paths against the controller's
# session directory, the one directory that holds both. The controller
# exports it in SESSION_ROOT_ENV; a worker accepts it only when it is the
# TMPDIR the worker inherited and a real "eqt-" directory of this user.
SESSION_ROOT_ENV = "EQUIPA_TEST_SESSION_ROOT"
_XDIST_WORKER_ID = re.compile(r"gw\d+")


def _xdist_session_root(worker_id: str | None, exported_root: str | None,
                        inherited_tmp: str) -> Path | None:
    """The controller's session directory if this process is one of its
    xdist workers, else None (a top-level session, serial or controller).

    A test that starts a Python subprocess inherits PYTEST_XDIST_WORKER but
    not the controller's TMPDIR (the worker moved it), so it stays top-level.
    """
    if not worker_id or not _XDIST_WORKER_ID.fullmatch(worker_id):
        return None
    if not exported_root or Path(exported_root) != Path(inherited_tmp):
        return None
    root = Path(exported_root)
    try:
        info = root.lstat()
    except OSError:
        return None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or not root.name.startswith(_SESSION_TMP_PREFIX)):
        return None
    return root


XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER") or None
_CONTROLLER_SESSION_ROOT = _xdist_session_root(
    XDIST_WORKER, os.environ.get(SESSION_ROOT_ENV), _ORIGINAL_TMP)
if _CONTROLLER_SESSION_ROOT is None:
    XDIST_WORKER = None

# A worker's parent temp directory is the controller's live session
# directory: never sweep it (nothing in it is a day old anyway).
if XDIST_WORKER is None:
    _sweep_stale_session_dirs(_ORIGINAL_TMP)
SESSION_TMP = Path(tempfile.mkdtemp(prefix=_SESSION_TMP_PREFIX,
                                    dir=_ORIGINAL_TMP))
os.environ["TMPDIR"] = str(SESSION_TMP)
tempfile.tempdir = str(SESSION_TMP)
# The directory every DB path and tmp_path of this run must sit inside.
SESSION_ROOT = _CONTROLLER_SESSION_ROOT or SESSION_TMP
os.environ[SESSION_ROOT_ENV] = str(SESSION_ROOT)

_TEST_DB_DIR = Path(tempfile.mkdtemp(
    prefix=f"equipa-test-db-{XDIST_WORKER}-" if XDIST_WORKER
    else "equipa-test-db-"))
TEST_DB_PATH = _TEST_DB_DIR / "theforge-test.db"
os.environ["THEFORGE_DB"] = str(TEST_DB_PATH)


def _is_safe_test_db(path) -> bool:
    """True only for a DB path that cannot be a real TheForge database.

    The path must not itself be a symlink, and after resolving any symlinks
    in its parents it must sit inside this run's session directory
    (SESSION_ROOT, in the system temp directory, where this conftest's DB
    and every tmp_path live; under xdist the controller's, holding every
    worker's). A repo-root theforge.db that is a symlink to production fails
    both tests. Nothing inside the repo counts as safe either, even when the
    checkout itself lives under /tmp: the repo-root default is never a test
    DB.
    """
    p = Path(path)
    if p.is_symlink():
        return False
    tmp_root = SESSION_ROOT.resolve()
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


def _remove_session_tmp() -> None:
    """Remove SESSION_TMP, every per-run config directory in it included.

    Tests leave read-only directories behind (permission probes), so an
    entry that cannot be removed gets its parent made writable once and is
    retried. Whatever still remains is reported, never silently kept.
    """
    def _retry_writable(func, path, _exc):
        parent = os.path.dirname(path)
        try:
            os.chmod(parent, 0o700)
            if os.path.isdir(path) and not os.path.islink(path):
                os.chmod(path, 0o700)
                shutil.rmtree(path, ignore_errors=True)
            else:
                func(path)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(SESSION_TMP, onexc=_retry_writable)
    else:
        shutil.rmtree(SESSION_TMP, onerror=_retry_writable)
    if SESSION_TMP.exists():
        print(f"  [conftest] WARNING: could not fully remove the session temp "
              f"directory {SESSION_TMP}", file=sys.stderr)


# The process that owns SESSION_TMP. A child forked by a test inherits the
# handler below but must never remove the session's directory.
_SESSION_PID = os.getpid()


def _remove_session_tmp_on_sigterm(signum, _frame) -> None:
    """Remove SESSION_TMP, then die of the signal as if it were unhandled.

    `timeout` stops a slow suite with SIGTERM, which skips
    pytest_unconfigure, so the session directory (with the per-run Claude
    config directories of the tests that ran) would stay behind.
    """
    if os.getpid() == _SESSION_PID:
        _remove_session_tmp()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


signal.signal(signal.SIGTERM, _remove_session_tmp_on_sigterm)


def pytest_unconfigure(config):
    """Remove this run's throwaway DB directory and the session temp
    directory (with every per-run Claude config directory in it)."""
    shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)
    _remove_session_tmp()
