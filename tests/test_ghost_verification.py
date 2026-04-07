"""Tests for ForgeSmith ghost finding verification (PHASE 2.95).

Verifies the pipeline that checks resolved security findings for ghost
vulnerabilities — findings marked resolved but still present in code.
"""

import json
import re
import sqlite3
import textwrap

import pytest

# Import under test
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forgesmith import (
    fetch_unverified_resolved_findings,
    load_ghost_skill_prompt,
    build_ghost_prompt,
    parse_ghost_verdict,
    apply_ghost_verdict,
    run_ghost_verification,
    GHOST_SKILL_PATH,
    MAX_GHOST_VERIFICATIONS_PER_RUN,
)


# --- Fixtures ---

@pytest.fixture
def memory_db(monkeypatch, tmp_path):
    """Create an in-memory SQLite database with decisions + tasks tables."""
    db_path = str(tmp_path / "test_ghost.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        )
    """)
    conn.execute("INSERT INTO projects (id, name) VALUES (23, 'Equipa')")
    conn.execute("""
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'todo',
            priority TEXT DEFAULT 'medium'
        )
    """)
    conn.execute("""
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            decision TEXT NOT NULL,
            rationale TEXT,
            alternatives_considered TEXT,
            decided_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            decision_type TEXT NOT NULL DEFAULT 'general',
            status TEXT NOT NULL DEFAULT 'open',
            resolved_by_task_id INTEGER DEFAULT NULL,
            verified_at DATETIME DEFAULT NULL
        )
    """)
    conn.commit()
    conn.close()

    # Patch THEFORGE_DB env var so get_db uses our test DB
    monkeypatch.setenv("THEFORGE_DB", db_path)
    return db_path


def _insert_finding(db_path, finding_id=None, topic="SQL injection in /api/users",
                    decision="Parameterized queries", rationale="Prevent SQLi",
                    alternatives="src/api/users.py:42", status="resolved",
                    decision_type="security_finding", verified_at=None,
                    project_id=23, resolved_by=None):
    """Helper to insert a test decision/finding."""
    conn = sqlite3.connect(db_path)
    if finding_id:
        conn.execute(
            """INSERT INTO decisions
               (id, project_id, topic, decision, rationale,
                alternatives_considered, decision_type, status,
                resolved_by_task_id, verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (finding_id, project_id, topic, decision, rationale,
             alternatives, decision_type, status, resolved_by, verified_at),
        )
    else:
        conn.execute(
            """INSERT INTO decisions
               (project_id, topic, decision, rationale,
                alternatives_considered, decision_type, status,
                resolved_by_task_id, verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, topic, decision, rationale,
             alternatives, decision_type, status, resolved_by, verified_at),
        )
    conn.commit()
    conn.close()


# --- Tests: fetch_unverified_resolved_findings ---

class TestFetchUnverifiedFindings:
    def test_returns_resolved_unverified(self, memory_db):
        _insert_finding(memory_db, finding_id=1)
        findings = fetch_unverified_resolved_findings()
        assert len(findings) == 1
        assert findings[0]["id"] == 1
        assert findings[0]["topic"] == "SQL injection in /api/users"

    def test_excludes_verified(self, memory_db):
        _insert_finding(memory_db, finding_id=1, verified_at="2025-01-01")
        findings = fetch_unverified_resolved_findings()
        assert len(findings) == 0

    def test_excludes_open_findings(self, memory_db):
        _insert_finding(memory_db, finding_id=1, status="open")
        findings = fetch_unverified_resolved_findings()
        assert len(findings) == 0

    def test_excludes_non_security_decisions(self, memory_db):
        _insert_finding(memory_db, finding_id=1, decision_type="general")
        findings = fetch_unverified_resolved_findings()
        assert len(findings) == 0

    def test_limits_to_max(self, memory_db):
        for i in range(10):
            _insert_finding(memory_db, finding_id=i + 1,
                            topic=f"Finding {i + 1}")
        findings = fetch_unverified_resolved_findings()
        assert len(findings) == MAX_GHOST_VERIFICATIONS_PER_RUN

    def test_empty_table(self, memory_db):
        findings = fetch_unverified_resolved_findings()
        assert findings == []


# --- Tests: load_ghost_skill_prompt ---

class TestLoadGhostSkillPrompt:
    def test_loads_and_strips_frontmatter(self):
        prompt = load_ghost_skill_prompt()
        assert prompt is not None
        assert "Ghost Finding Verification" in prompt
        # Should NOT contain YAML frontmatter
        assert "allowed-tools:" not in prompt

    def test_contains_verdict_format(self):
        prompt = load_ghost_skill_prompt()
        assert "VERDICT: VERIFIED" in prompt
        assert "VERDICT: STILL_PRESENT" in prompt


# --- Tests: build_ghost_prompt ---

class TestBuildGhostPrompt:
    def test_injects_finding_details(self):
        skill_prompt = "# Ghost Verification\nVerify the finding below."
        finding = {
            "id": 42,
            "topic": "XSS in search endpoint",
            "decision": "Added DOMPurify sanitization",
            "rationale": "Prevents reflected XSS",
            "alternatives_considered": "src/search.py:88",
            "resolved_by_task_id": 100,
        }
        result = build_ghost_prompt(skill_prompt, finding)
        assert "XSS in search endpoint" in result
        assert "DOMPurify sanitization" in result
        assert "src/search.py:88" in result
        assert "#100" in result
        assert "Ghost Verification" in result

    def test_handles_missing_fields(self):
        skill_prompt = "Verify."
        finding = {
            "id": 1,
            "topic": "Issue",
            "decision": "Fixed",
            "rationale": "",
            "alternatives_considered": None,
            "resolved_by_task_id": None,
        }
        result = build_ghost_prompt(skill_prompt, finding)
        assert "Issue" in result
        assert "Resolved by task" not in result


# --- Tests: parse_ghost_verdict ---

class TestParseGhostVerdict:
    def test_parses_verified(self):
        output = textwrap.dedent("""\
            I checked the code and the fix is complete.

            VERDICT: VERIFIED
            EVIDENCE: The query now uses parameterized statements at line 42.
        """)
        verdict, evidence, severity = parse_ghost_verdict(output)
        assert verdict == "VERIFIED"
        assert "parameterized" in evidence
        assert severity is None

    def test_parses_still_present(self):
        output = textwrap.dedent("""\
            The vulnerability remains.

            VERDICT: STILL_PRESENT
            EVIDENCE: String concatenation still used in build_query() at line 55.
            SEVERITY: high
        """)
        verdict, evidence, severity = parse_ghost_verdict(output)
        assert verdict == "STILL_PRESENT"
        assert "String concatenation" in evidence
        assert severity == "high"

    def test_still_present_defaults_severity_to_high(self):
        output = "VERDICT: STILL_PRESENT\nEVIDENCE: Still broken."
        verdict, evidence, severity = parse_ghost_verdict(output)
        assert verdict == "STILL_PRESENT"
        assert severity == "high"

    def test_returns_none_on_missing_verdict(self):
        output = "I looked at the code but couldn't determine the status."
        verdict, evidence, severity = parse_ghost_verdict(output)
        assert verdict is None
        assert evidence is None

    def test_case_insensitive_verdict(self):
        output = "verdict: verified\nevidence: Fixed."
        verdict, evidence, severity = parse_ghost_verdict(output)
        assert verdict == "VERIFIED"

    def test_parses_severity_levels(self):
        for level in ("critical", "high", "medium", "low"):
            output = f"VERDICT: STILL_PRESENT\nEVIDENCE: Bug.\nSEVERITY: {level}"
            _, _, severity = parse_ghost_verdict(output)
            assert severity == level


# --- Tests: apply_ghost_verdict ---

class TestApplyGhostVerdict:
    def test_verified_sets_timestamp(self, memory_db):
        _insert_finding(memory_db, finding_id=10)
        finding = {"id": 10, "project_id": 23, "topic": "Test"}

        result = apply_ghost_verdict(finding, "VERIFIED", "Fix confirmed.", None)

        assert result is not None
        assert result["verdict"] == "VERIFIED"
        assert result["action"] == "marked_verified"

        # Verify in DB
        conn = sqlite3.connect(memory_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT verified_at FROM decisions WHERE id = 10"
        ).fetchone()
        conn.close()
        assert row["verified_at"] is not None

    def test_still_present_creates_task(self, memory_db):
        _insert_finding(memory_db, finding_id=20)
        finding = {"id": 20, "project_id": 23, "topic": "Path traversal in upload"}

        result = apply_ghost_verdict(
            finding, "STILL_PRESENT", "User input unsanitized.", "high"
        )

        assert result is not None
        assert result["verdict"] == "STILL_PRESENT"
        assert result["new_task_id"] is not None
        assert result["severity"] == "high"

        # Verify decision status changed
        conn = sqlite3.connect(memory_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM decisions WHERE id = 20"
        ).fetchone()
        assert row["status"] == "failed_resolution"

        # Verify task created
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (result["new_task_id"],)
        ).fetchone()
        conn.close()
        assert task is not None
        assert task["priority"] == "high"
        assert "Ghost finding" in task["title"]
        assert "STILL PRESENT" in task["description"]


# --- Tests: run_ghost_verification (integration) ---

class TestRunGhostVerification:
    def test_no_findings_returns_empty(self, memory_db):
        result = run_ghost_verification(dry_run=False)
        assert result["findings_checked"] == 0
        assert result["verified"] == 0
        assert result["still_present"] == 0

    def test_dry_run_skips_dispatch(self, memory_db):
        _insert_finding(memory_db, finding_id=1)
        _insert_finding(memory_db, finding_id=2, topic="XSS in /search")

        result = run_ghost_verification(dry_run=True)
        assert result["findings_checked"] == 2
        assert result["verified"] == 0
        assert result["still_present"] == 0
        assert len(result["details"]) == 2
        assert all(d["action"] == "dry_run_skipped" for d in result["details"])

    def test_dispatch_failure_counted(self, memory_db, monkeypatch):
        _insert_finding(memory_db, finding_id=1)

        # Mock dispatch to return None (failure)
        monkeypatch.setattr(
            "forgesmith.dispatch_ghost_scout", lambda p: None
        )
        result = run_ghost_verification(dry_run=False)
        assert result["dispatch_failures"] == 1
        assert result["findings_checked"] == 1

    def test_verified_flow(self, memory_db, monkeypatch):
        _insert_finding(memory_db, finding_id=1)

        monkeypatch.setattr(
            "forgesmith.dispatch_ghost_scout",
            lambda p: "VERDICT: VERIFIED\nEVIDENCE: Fix confirmed.",
        )
        result = run_ghost_verification(dry_run=False)
        assert result["verified"] == 1
        assert result["still_present"] == 0

        # Check DB
        conn = sqlite3.connect(memory_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT verified_at FROM decisions WHERE id = 1"
        ).fetchone()
        conn.close()
        assert row["verified_at"] is not None

    def test_still_present_flow(self, memory_db, monkeypatch):
        _insert_finding(memory_db, finding_id=1)

        monkeypatch.setattr(
            "forgesmith.dispatch_ghost_scout",
            lambda p: "VERDICT: STILL_PRESENT\nEVIDENCE: Bug remains.\nSEVERITY: critical",
        )
        result = run_ghost_verification(dry_run=False)
        assert result["still_present"] == 1
        assert result["verified"] == 0

        # Check task was created
        conn = sqlite3.connect(memory_db)
        conn.row_factory = sqlite3.Row
        tasks = conn.execute("SELECT * FROM tasks").fetchall()
        conn.close()
        assert len(tasks) == 1
        assert tasks[0]["priority"] == "high"

    def test_parse_failure_counted(self, memory_db, monkeypatch):
        _insert_finding(memory_db, finding_id=1)

        monkeypatch.setattr(
            "forgesmith.dispatch_ghost_scout",
            lambda p: "I couldn't determine the verdict.",
        )
        result = run_ghost_verification(dry_run=False)
        assert result["parse_failures"] == 1
        assert result["findings_checked"] == 1
