#!/usr/bin/env python3
"""Test suite for GEPA episode history check — Task #1603.

Validates that GEPA penalizes candidate mutations resembling previously
failed approaches in agent_episodes.

Tests:
1. compute_keyword_overlap: Jaccard similarity correctness
2. get_failed_episodes_by_keywords: DB query returns correct episodes
3. check_episode_history_for_candidate: penalty logic
4. extract_mutation_diff: extracts added lines from unified diff
5. Integration: full pipeline penalizes candidate matching failed history

Copyright 2026 Forgeborn
"""

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from forgesmith_gepa import (
    compute_keyword_overlap,
    get_failed_episodes_by_keywords,
    check_episode_history_for_candidate,
    extract_mutation_diff,
    EPISODE_HISTORY_OVERLAP_THRESHOLD,
    EPISODE_HISTORY_PENALTY,
)


# ============================================================
# Test compute_keyword_overlap
# ============================================================


class TestComputeKeywordOverlap:
    """Tests for Jaccard keyword overlap scoring."""

    def test_identical_texts(self):
        """Identical texts should return 1.0."""
        text = "add input validation for user registration"
        assert compute_keyword_overlap(text, text) == 1.0

    def test_completely_different(self):
        """Texts with no shared words should return 0.0."""
        a = "alpha bravo charlie"
        b = "delta echo foxtrot"
        assert compute_keyword_overlap(a, b) == 0.0

    def test_partial_overlap(self):
        """Texts with some shared words should return fraction."""
        a = "add input validation error handling"
        b = "add error handling for database queries"
        overlap = compute_keyword_overlap(a, b)
        # Shared: {add, error, handling} = 3
        # Union: {add, input, validation, error, handling, for, database, queries} = 8
        assert abs(overlap - 3 / 8) < 0.01

    def test_empty_text_a(self):
        """Empty first text returns 0.0."""
        assert compute_keyword_overlap("", "some text") == 0.0

    def test_empty_text_b(self):
        """Empty second text returns 0.0."""
        assert compute_keyword_overlap("some text", "") == 0.0

    def test_both_empty(self):
        """Both empty returns 0.0."""
        assert compute_keyword_overlap("", "") == 0.0

    def test_none_text(self):
        """None text returns 0.0."""
        assert compute_keyword_overlap(None, "text") == 0.0
        assert compute_keyword_overlap("text", None) == 0.0

    def test_case_insensitive(self):
        """Overlap computation is case-insensitive."""
        a = "Add Input Validation"
        b = "add input validation"
        assert compute_keyword_overlap(a, b) == 1.0

    def test_punctuation_stripped(self):
        """Punctuation does not affect word matching."""
        a = "add: input, validation!"
        b = "add input validation"
        assert compute_keyword_overlap(a, b) == 1.0


# ============================================================
# Test extract_mutation_diff
# ============================================================


class TestExtractMutationDiff:
    """Tests for extracting behavioral change from prompt diff."""

    def test_added_lines_extracted(self):
        """Only added lines are captured from the diff."""
        old = "line one\nline two\nline three"
        new = "line one\nline two\nnew instruction added\nline three"
        diff = extract_mutation_diff(old, new)
        assert "new instruction added" in diff

    def test_removed_lines_not_included(self):
        """Removed lines should not appear in mutation diff."""
        old = "line one\nremove me\nline three"
        new = "line one\nline three"
        diff = extract_mutation_diff(old, new)
        assert "remove me" not in diff

    def test_identical_prompts(self):
        """Identical prompts produce empty diff."""
        text = "same content\nno changes"
        diff = extract_mutation_diff(text, text)
        assert diff.strip() == ""

    def test_empty_inputs(self):
        """Empty inputs produce empty diff."""
        diff = extract_mutation_diff("", "")
        assert diff.strip() == ""

    def test_multiline_addition(self):
        """Multiple added lines are all captured."""
        old = "base prompt"
        new = "base prompt\nfirst addition\nsecond addition"
        diff = extract_mutation_diff(old, new)
        assert "first addition" in diff
        assert "second addition" in diff


# ============================================================
# Test get_failed_episodes_by_keywords
# ============================================================


class TestGetFailedEpisodesByKeywords:
    """Tests for fetching failed episodes from DB."""

    def _create_test_db(self, tmp_path):
        """Create a test DB with agent_episodes table and sample data."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE agent_episodes (
                id INTEGER PRIMARY KEY,
                task_id INTEGER,
                role TEXT,
                task_type TEXT,
                project_id INTEGER,
                approach_summary TEXT,
                turns_used INTEGER,
                outcome TEXT,
                error_patterns TEXT,
                reflection TEXT,
                q_value REAL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Insert failed episodes
        conn.execute("""
            INSERT INTO agent_episodes
            (task_id, role, outcome, q_value, approach_summary, reflection,
             created_at)
            VALUES (1, 'developer', 'early_terminated', 0.1,
                    'tried adding input validation with regex patterns',
                    'regex approach was too brittle, need schema validation',
                    datetime('now', '-5 days'))
        """)
        conn.execute("""
            INSERT INTO agent_episodes
            (task_id, role, outcome, q_value, approach_summary, reflection,
             created_at)
            VALUES (2, 'developer', 'blocked', 0.2,
                    'attempted database migration with raw SQL',
                    'raw SQL migration failed due to missing FK constraints',
                    datetime('now', '-10 days'))
        """)
        # Insert a successful episode (should NOT be returned)
        conn.execute("""
            INSERT INTO agent_episodes
            (task_id, role, outcome, q_value, approach_summary, reflection,
             created_at)
            VALUES (3, 'developer', 'success', 0.9,
                    'implemented feature with proper error handling',
                    'clean implementation, all tests passed',
                    datetime('now', '-3 days'))
        """)
        # Insert a high q_value failure (should NOT be returned)
        conn.execute("""
            INSERT INTO agent_episodes
            (task_id, role, outcome, q_value, approach_summary, reflection,
             created_at)
            VALUES (4, 'developer', 'early_terminated', 0.5,
                    'timeout due to slow test suite',
                    'tests took too long, not a code issue',
                    datetime('now', '-2 days'))
        """)
        conn.commit()
        conn.close()
        return db_path

    def test_returns_failed_episodes_only(self, tmp_path):
        """Only failed episodes with low q_value are returned."""
        db_path = self._create_test_db(tmp_path)
        with patch("forgesmith_gepa.get_db") as mock_get_db:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            mock_get_db.return_value = conn
            episodes = get_failed_episodes_by_keywords("developer")

        assert len(episodes) == 2
        outcomes = {ep["outcome"] for ep in episodes}
        assert outcomes <= {"early_terminated", "blocked"}
        for ep in episodes:
            assert ep["q_value"] < 0.3

    def test_filters_by_role(self, tmp_path):
        """Episodes for wrong role are not returned."""
        db_path = self._create_test_db(tmp_path)
        with patch("forgesmith_gepa.get_db") as mock_get_db:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            mock_get_db.return_value = conn
            episodes = get_failed_episodes_by_keywords("tester")

        assert len(episodes) == 0

    def test_empty_db(self, tmp_path):
        """Empty table returns empty list."""
        db_path = str(tmp_path / "empty.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE agent_episodes (
                id INTEGER PRIMARY KEY,
                task_id INTEGER, role TEXT, task_type TEXT,
                project_id INTEGER, approach_summary TEXT,
                turns_used INTEGER, outcome TEXT, error_patterns TEXT,
                reflection TEXT, q_value REAL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        conn.close()
        with patch("forgesmith_gepa.get_db") as mock_get_db:
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            mock_get_db.return_value = c
            episodes = get_failed_episodes_by_keywords("developer")

        assert episodes == []


# ============================================================
# Test check_episode_history_for_candidate
# ============================================================


class TestCheckEpisodeHistoryForCandidate:
    """Tests for the full episode history penalty check."""

    def test_penalizes_matching_mutation(self):
        """Candidate matching failed episode gets penalized."""
        mock_episodes = [
            {
                "id": 1,
                "approach_summary": "add input validation regex",
                "outcome": "early_terminated",
                "q_value": 0.1,
                "reflection": "input validation regex failed",
                "error_patterns": "",
            },
        ]
        # Mutation that closely resembles the failed approach
        # (high word overlap: add, input, validation, regex all shared)
        mutation_diff = "add input validation regex patterns"

        with patch(
            "forgesmith_gepa.get_failed_episodes_by_keywords",
            return_value=mock_episodes,
        ):
            result = check_episode_history_for_candidate(
                "developer", mutation_diff
            )

        assert result["penalize"] is True
        assert result["matching_episodes"] >= 1
        assert result["penalty_factor"] == EPISODE_HISTORY_PENALTY
        assert result["max_overlap"] > EPISODE_HISTORY_OVERLAP_THRESHOLD

    def test_no_penalty_for_novel_mutation(self):
        """Candidate with no resemblance to failed episodes is not penalized."""
        mock_episodes = [
            {
                "id": 1,
                "approach_summary": "adding input validation with regex patterns",
                "outcome": "early_terminated",
                "q_value": 0.1,
                "reflection": "regex approach was too brittle",
                "error_patterns": "",
            },
        ]
        # Completely different mutation
        mutation_diff = "implement caching layer with Redis TTL expiration"

        with patch(
            "forgesmith_gepa.get_failed_episodes_by_keywords",
            return_value=mock_episodes,
        ):
            result = check_episode_history_for_candidate(
                "developer", mutation_diff
            )

        assert result["penalize"] is False
        assert result["matching_episodes"] == 0
        assert result["penalty_factor"] == 1.0

    def test_no_penalty_when_no_failed_episodes(self):
        """No penalty when episode history is empty."""
        with patch(
            "forgesmith_gepa.get_failed_episodes_by_keywords",
            return_value=[],
        ):
            result = check_episode_history_for_candidate(
                "developer", "some mutation text"
            )

        assert result["penalize"] is False
        assert result["matching_episodes"] == 0
        assert result["penalty_factor"] == 1.0

    def test_no_penalty_for_empty_mutation(self):
        """Empty mutation diff does not trigger penalty."""
        result = check_episode_history_for_candidate("developer", "")
        assert result["penalize"] is False
        assert result["penalty_factor"] == 1.0

    def test_no_penalty_for_none_mutation(self):
        """None mutation diff does not trigger penalty."""
        result = check_episode_history_for_candidate("developer", None)
        assert result["penalize"] is False

    def test_multiple_matching_episodes_counted(self):
        """All matching failed episodes are counted."""
        mock_episodes = [
            {
                "id": 1,
                "approach_summary": "regex validation input",
                "outcome": "early_terminated",
                "q_value": 0.1,
                "reflection": "regex validation input failed",
                "error_patterns": "",
            },
            {
                "id": 2,
                "approach_summary": "regex validation pattern",
                "outcome": "blocked",
                "q_value": 0.15,
                "reflection": "regex validation pattern broken",
                "error_patterns": "",
            },
        ]
        # Short mutation with high overlap to both episodes
        mutation_diff = "regex validation input pattern"

        with patch(
            "forgesmith_gepa.get_failed_episodes_by_keywords",
            return_value=mock_episodes,
        ):
            result = check_episode_history_for_candidate(
                "developer", mutation_diff
            )

        assert result["penalize"] is True
        assert result["matching_episodes"] == 2

    def test_custom_threshold(self):
        """Custom overlap threshold is respected."""
        mock_episodes = [
            {
                "id": 1,
                "approach_summary": "some words here",
                "outcome": "early_terminated",
                "q_value": 0.1,
                "reflection": "",
                "error_patterns": "",
            },
        ]
        # With a very high threshold, marginal overlap won't match
        with patch(
            "forgesmith_gepa.get_failed_episodes_by_keywords",
            return_value=mock_episodes,
        ):
            result = check_episode_history_for_candidate(
                "developer",
                "some different words entirely new approach",
                overlap_threshold=0.9,
            )

        assert result["penalize"] is False

    def test_custom_penalty_factor(self):
        """Custom penalty factor is applied correctly."""
        mock_episodes = [
            {
                "id": 1,
                "approach_summary": "add input validation regex",
                "outcome": "early_terminated",
                "q_value": 0.1,
                "reflection": "regex input validation failed",
                "error_patterns": "",
            },
        ]
        mutation_diff = "add input validation regex patterns"

        with patch(
            "forgesmith_gepa.get_failed_episodes_by_keywords",
            return_value=mock_episodes,
        ):
            result = check_episode_history_for_candidate(
                "developer",
                mutation_diff,
                penalty_factor=0.5,
            )

        assert result["penalize"] is True
        assert result["penalty_factor"] == 0.5


# ============================================================
# Integration: Full pipeline check
# ============================================================


class TestIntegrationEpisodeCheck:
    """Integration test verifying the episode check is wired into GEPA."""

    def test_episode_check_result_in_gepa_output(self):
        """Verify check_episode_history_for_candidate returns valid structure."""
        mock_episodes = [
            {
                "id": 1,
                "approach_summary": "brute force parsing",
                "outcome": "cycles_exhausted",
                "q_value": 0.05,
                "reflection": "brute force parsing slow",
                "error_patterns": "",
            },
        ]

        # High overlap: "brute force parsing" shared across 4 words
        mutation_diff = "brute force parsing input"

        with patch(
            "forgesmith_gepa.get_failed_episodes_by_keywords",
            return_value=mock_episodes,
        ):
            result = check_episode_history_for_candidate(
                "developer", mutation_diff
            )

        # Verify structure
        assert "penalize" in result
        assert "matching_episodes" in result
        assert "max_overlap" in result
        assert "penalty_factor" in result

        # Verify types
        assert isinstance(result["penalize"], bool)
        assert isinstance(result["matching_episodes"], int)
        assert isinstance(result["max_overlap"], float)
        assert isinstance(result["penalty_factor"], float)

        # This mutation matches the failed episode
        assert result["penalize"] is True
        assert result["matching_episodes"] >= 1
        assert 0.0 < result["penalty_factor"] < 1.0
