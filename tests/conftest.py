"""Shared pytest fixtures for EQUIPA test suite.

Ensures database schema exists before each test module that needs it.
Some tests create temp DBs and reset _SCHEMA_ENSURED, so this fixture
runs at module scope to re-create tables when needed.

Copyright 2026 Forgeborn
"""

import sys
from pathlib import Path

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True, scope="module")
def ensure_test_db_schema():
    """Create all required tables in the test DB before each test module.

    Uses module scope because some tests (test_agent_messages, test_rubric_scoring)
    swap THEFORGE_DB to a temp path and set _SCHEMA_ENSURED = True, which prevents
    ensure_schema() from running for subsequent modules. Resetting per-module
    guarantees that each test file starts with a valid schema.
    """
    import forge_orchestrator

    # Reset the flag so ensure_schema() actually creates tables
    forge_orchestrator._SCHEMA_ENSURED = False
    forge_orchestrator.ensure_schema()
