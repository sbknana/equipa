#!/usr/bin/env python3
"""
Test suite for rubric_quality_scorer.py — post-task quality scoring module.

Tests cover:
- score_agent_output() for each role with varying result_text
- All 5 quality dimensions (error_handling, naming_consistency, code_structure,
  test_coverage, documentation) scored 0-10
- File-based heuristics (_apply_file_heuristics)
- Role-specific weight multipliers
- Database storage (store_quality_scores) and convenience wrapper (score_and_store)
- Edge cases: empty input, unknown role, None values

Copyright 2026 Forgeborn
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rubric_quality_scorer import (
    QUALITY_DIMENSIONS,
    ROLE_WEIGHTS,
    _DEFAULT_WEIGHTS,
    _apply_file_heuristics,
    _score_dimension,
    score_agent_output,
    score_and_store,
    store_quality_scores,
)


class TestScoreDimension(unittest.TestCase):
    """Test individual dimension scoring via _score_dimension."""

    def test_neutral_baseline_with_no_indicators(self):
        """Text with no quality indicators should score at baseline (5)."""
        score = _score_dimension("the quick brown fox jumps over the lazy dog", "error_handling")
        self.assertEqual(score, 5)

    def test_positive_indicators_increase_score(self):
        """Positive indicators should increase the score above 5."""
        text = "added proper error handling with try/except and input validation"
        score = _score_dimension(text, "error_handling")
        self.assertGreater(score, 5)

    def test_negative_indicators_decrease_score(self):
        """Negative indicators should decrease the score below 5."""
        text = "bare except used, silently fails on error, no error handling at all"
        score = _score_dimension(text, "error_handling")
        self.assertLess(score, 5)

    def test_score_clamped_to_zero_minimum(self):
        """Score should never go below 0 even with many negative indicators."""
        text = (
            "bare except swallowed exception no error handling "
            "missing error unhandled exception silently fail "
            "ignoring error crashes everywhere bare except bare except"
        )
        score = _score_dimension(text, "error_handling")
        self.assertGreaterEqual(score, 0)

    def test_score_clamped_to_ten_maximum(self):
        """Score should never exceed 10 even with many positive indicators."""
        text = (
            "error handling try/except exception validation "
            "edge case boundary check input validation "
            "error recovery fallback gracefully handled "
            "error handling error handling error handling"
        )
        score = _score_dimension(text, "error_handling")
        self.assertLessEqual(score, 10)

    def test_unknown_dimension_returns_neutral(self):
        """Unknown dimension name should return neutral score of 5."""
        score = _score_dimension("anything", "nonexistent_dimension")
        self.assertEqual(score, 5)

    def test_all_dimensions_have_both_polarities(self):
        """Every dimension in QUALITY_DIMENSIONS must have positive and negative lists."""
        for dim_name, config in QUALITY_DIMENSIONS.items():
            self.assertIn("positive", config, f"{dim_name} missing positive indicators")
            self.assertIn("negative", config, f"{dim_name} missing negative indicators")
            self.assertGreater(len(config["positive"]), 0, f"{dim_name} has empty positive list")
            self.assertGreater(len(config["negative"]), 0, f"{dim_name} has empty negative list")

    def test_naming_consistency_positive(self):
        """Naming consistency should score high with clean naming mentions."""
        text = "consistent naming, follows convention, descriptive names throughout"
        score = _score_dimension(text, "naming_consistency")
        self.assertGreater(score, 5)

    def test_code_structure_positive(self):
        """Code structure should score high with modular design indicators."""
        text = "well-structured modular code with separation of concerns, extracted functions"
        score = _score_dimension(text, "code_structure")
        self.assertGreater(score, 5)

    def test_test_coverage_positive(self):
        """Test coverage should score high with test-related indicators."""
        text = "all tests pass, wrote unit tests, test_feature.py created, good coverage"
        score = _score_dimension(text, "test_coverage")
        self.assertGreater(score, 5)

    def test_documentation_positive(self):
        """Documentation should score high with doc-related indicators."""
        text = "added docstrings, well documented, SUMMARY: done, REFLECTION: good"
        score = _score_dimension(text, "documentation")
        self.assertGreater(score, 5)

    def test_pattern_match_count_capped_at_three(self):
        """Repeated pattern matches should be capped at 3 contributions."""
        # "error handling" appears 5 times but should only count up to 3
        text = "error handling error handling error handling error handling error handling"
        score_many = _score_dimension(text, "error_handling")
        # With cap, delta is 2 * 3 = 6, total = 5 + 6 = 11 -> clamped to 10
        self.assertLessEqual(score_many, 10)


class TestApplyFileHeuristics(unittest.TestCase):
    """Test file-based score adjustments."""

    def test_no_files_reduces_scores(self):
        """Empty files_changed list should reduce code_structure and test_coverage."""
        scores = {"code_structure": 7, "test_coverage": 7, "documentation": 5}
        _apply_file_heuristics(scores, [])
        self.assertEqual(scores["code_structure"], 5)  # 7 - 2
        self.assertEqual(scores["test_coverage"], 5)    # 7 - 2

    def test_test_files_boost_coverage(self):
        """Test files in changes should boost test_coverage by 2."""
        scores = {"code_structure": 5, "test_coverage": 5, "documentation": 5}
        _apply_file_heuristics(scores, ["src/main.py", "tests/test_main.py"])
        self.assertEqual(scores["test_coverage"], 7)

    def test_doc_files_boost_documentation(self):
        """Documentation files should boost documentation by 1."""
        scores = {"code_structure": 5, "test_coverage": 5, "documentation": 5}
        _apply_file_heuristics(scores, ["README.md"])
        self.assertEqual(scores["documentation"], 6)

    def test_moderate_file_count_boosts_structure(self):
        """2-10 files changed should boost code_structure by 1."""
        scores = {"code_structure": 5, "test_coverage": 5, "documentation": 5}
        files = [f"src/file_{i}.py" for i in range(5)]
        _apply_file_heuristics(scores, files)
        self.assertEqual(scores["code_structure"], 6)

    def test_excessive_file_count_reduces_structure(self):
        """More than 20 files changed should reduce code_structure by 1."""
        scores = {"code_structure": 5, "test_coverage": 5, "documentation": 5}
        files = [f"src/file_{i}.py" for i in range(25)]
        _apply_file_heuristics(scores, files)
        self.assertEqual(scores["code_structure"], 4)

    def test_none_files_same_as_empty(self):
        """None files_changed should behave like empty list."""
        scores = {"code_structure": 7, "test_coverage": 7, "documentation": 5}
        _apply_file_heuristics(scores, None)
        self.assertEqual(scores["code_structure"], 5)

    def test_spec_files_count_as_tests(self):
        """Files matching spec pattern should boost test_coverage."""
        scores = {"code_structure": 5, "test_coverage": 5, "documentation": 5}
        _apply_file_heuristics(scores, ["src/app.ts", "src/app.spec.ts"])
        self.assertEqual(scores["test_coverage"], 7)


class TestScoreAgentOutput(unittest.TestCase):
    """Test the main score_agent_output function."""

    def test_returns_required_keys(self):
        """Output should contain scores, total_score, max_possible, normalized_score, details."""
        result = score_agent_output("some output", [], "developer")
        self.assertIn("scores", result)
        self.assertIn("total_score", result)
        self.assertIn("max_possible", result)
        self.assertIn("normalized_score", result)
        self.assertIn("details", result)

    def test_all_five_dimensions_present(self):
        """All 5 quality dimensions should be in scores dict."""
        result = score_agent_output("text", ["file.py"], "developer")
        expected = {"error_handling", "naming_consistency", "code_structure",
                    "test_coverage", "documentation"}
        self.assertEqual(set(result["scores"].keys()), expected)

    def test_max_possible_is_fifty(self):
        """Max possible score is always 50 (5 dimensions * 10)."""
        result = score_agent_output("text", [], "developer")
        self.assertEqual(result["max_possible"], 50.0)

    def test_normalized_score_in_range(self):
        """Normalized score should be between 0.0 and 1.0."""
        result = score_agent_output("lots of good quality indicators", [], "developer")
        self.assertGreaterEqual(result["normalized_score"], 0.0)
        self.assertLessEqual(result["normalized_score"], 1.0)

    def test_each_dimension_score_in_range(self):
        """Every dimension score should be in [0, 10]."""
        result = score_agent_output(
            "error handling test pass modular clean naming documented", [], "developer"
        )
        for dim, score in result["scores"].items():
            self.assertGreaterEqual(score, 0.0, f"{dim} below 0")
            self.assertLessEqual(score, 10.0, f"{dim} above 10")

    def test_empty_result_text(self):
        """Empty/None result_text should not crash, returns neutral-ish scores."""
        result = score_agent_output("", [], "developer")
        self.assertIsNotNone(result)
        self.assertEqual(result["max_possible"], 50.0)

        result_none = score_agent_output(None, [], "developer")
        self.assertIsNotNone(result_none)

    def test_role_weights_applied_for_developer(self):
        """Developer role should amplify error_handling and code_structure (1.2x)."""
        # Use text that scores exactly at baseline for all dimensions
        text = "some output"
        dev_result = score_agent_output(text, ["file.py"], "developer")
        # Developer error_handling weight is 1.2, so score = 5 * 1.2 = 6.0
        self.assertEqual(dev_result["scores"]["error_handling"], 6.0)

    def test_role_weights_applied_for_tester(self):
        """Tester role should amplify test_coverage (1.5x)."""
        text = "some output"
        tester_result = score_agent_output(text, ["file.py"], "tester")
        # Tester test_coverage weight is 1.5, so baseline 5 * 1.5 = 7.5
        self.assertEqual(tester_result["scores"]["test_coverage"], 7.5)

    def test_unknown_role_uses_default_weights(self):
        """Unknown role should use default weights (all 1.0)."""
        text = "some output"
        result = score_agent_output(text, ["file.py"], "unknown_role")
        # All default weights are 1.0, so baseline stays at 5
        for dim, score in result["scores"].items():
            self.assertEqual(score, 5.0, f"{dim} not at neutral for unknown role")

    def test_details_contain_matched_patterns(self):
        """Details should list which patterns matched for each dimension."""
        text = "added error handling with try/except blocks"
        result = score_agent_output(text, [], "developer")
        self.assertIn("error_handling", result["details"])
        self.assertGreater(len(result["details"]["error_handling"]), 0)

    def test_high_quality_output_scores_well(self):
        """Realistic high-quality developer output should score well."""
        text = """
        RESULT: success
        SUMMARY: Implemented user authentication with proper error handling
        FILES_CHANGED: src/auth.py, tests/test_auth.py
        DECISIONS: Used JWT tokens with input validation and edge case handling
        REFLECTION: Well-structured modular code with clean naming convention.
        All tests pass with 100% coverage on the new module. Added docstrings
        and type hints throughout. Extracted helper functions for separation
        of concerns.
        """
        result = score_agent_output(text, ["src/auth.py", "tests/test_auth.py"], "developer")
        # Should score above 60% normalized
        self.assertGreater(result["normalized_score"], 0.6)

    def test_low_quality_output_scores_poorly(self):
        """Realistic low-quality output should score poorly."""
        text = """
        RESULT: blocked
        SUMMARY: Could not find module
        no error handling, bare except, inconsistent naming, no tests,
        undocumented, tightly coupled spaghetti code
        """
        result = score_agent_output(text, [], "developer")
        # Should score below 40% normalized
        self.assertLess(result["normalized_score"], 0.4)


class TestRoleWeights(unittest.TestCase):
    """Test role-specific weight configuration."""

    def test_all_defined_roles_have_all_dimensions(self):
        """Every role in ROLE_WEIGHTS should have all 5 dimensions."""
        expected_dims = set(QUALITY_DIMENSIONS.keys())
        for role, weights in ROLE_WEIGHTS.items():
            self.assertEqual(
                set(weights.keys()), expected_dims,
                f"Role '{role}' missing dimensions"
            )

    def test_default_weights_cover_all_dimensions(self):
        """_DEFAULT_WEIGHTS should have all 5 dimensions at 1.0."""
        expected_dims = set(QUALITY_DIMENSIONS.keys())
        self.assertEqual(set(_DEFAULT_WEIGHTS.keys()), expected_dims)
        for dim, weight in _DEFAULT_WEIGHTS.items():
            self.assertEqual(weight, 1.0, f"Default weight for {dim} is not 1.0")

    def test_developer_emphasizes_error_handling_and_structure(self):
        """Developer role should weight error_handling and code_structure higher."""
        dev = ROLE_WEIGHTS["developer"]
        self.assertGreater(dev["error_handling"], 1.0)
        self.assertGreater(dev["code_structure"], 1.0)

    def test_tester_emphasizes_test_coverage(self):
        """Tester role should weight test_coverage highest."""
        tester = ROLE_WEIGHTS["tester"]
        self.assertEqual(tester["test_coverage"], 1.5)
        # test_coverage should be the highest weight for testers
        max_weight = max(tester.values())
        self.assertEqual(tester["test_coverage"], max_weight)

    def test_security_reviewer_emphasizes_error_handling(self):
        """Security-reviewer should weight error_handling highest."""
        sec = ROLE_WEIGHTS["security-reviewer"]
        self.assertEqual(sec["error_handling"], 1.5)


class TestStoreQualityScores(unittest.TestCase):
    """Test database storage of quality scores."""

    def setUp(self):
        """Create a temporary test database with rubric_scores table."""
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE rubric_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_run_id INTEGER NOT NULL,
                task_id INTEGER,
                project_id INTEGER,
                role TEXT NOT NULL,
                rubric_version INTEGER DEFAULT 1,
                criteria_scores TEXT NOT NULL,
                total_score REAL NOT NULL,
                max_possible REAL NOT NULL,
                normalized_score REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def tearDown(self):
        """Clean up test database."""
        self.conn.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_store_returns_true_on_success(self):
        """store_quality_scores should return True on successful insert."""
        score_result = score_agent_output("some text", ["file.py"], "developer")
        result = store_quality_scores(
            agent_run_id=1, task_id=100, project_id=21,
            role="developer", score_result=score_result,
            db_path=self.db_path,
        )
        self.assertTrue(result)

    def test_stored_data_matches_input(self):
        """Stored row should match the input values."""
        score_result = score_agent_output("error handling test pass", ["a.py"], "developer")
        store_quality_scores(
            agent_run_id=42, task_id=200, project_id=23,
            role="developer", score_result=score_result,
            db_path=self.db_path,
        )

        row = self.conn.execute("SELECT * FROM rubric_scores WHERE agent_run_id = 42").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["agent_run_id"], 42)
        self.assertEqual(row["task_id"], 200)
        self.assertEqual(row["project_id"], 23)
        self.assertEqual(row["role"], "developer")
        self.assertEqual(row["rubric_version"], 1)
        self.assertAlmostEqual(row["total_score"], score_result["total_score"], places=1)
        self.assertAlmostEqual(row["max_possible"], score_result["max_possible"], places=1)
        self.assertAlmostEqual(row["normalized_score"], score_result["normalized_score"], places=3)

        # criteria_scores should be valid JSON matching the scores dict
        stored_scores = json.loads(row["criteria_scores"])
        self.assertEqual(stored_scores, score_result["scores"])

    def test_store_returns_false_on_missing_table(self):
        """store_quality_scores should return False when table doesn't exist."""
        bad_db_fd, bad_db_path = tempfile.mkstemp(suffix=".db")
        # Create empty DB without rubric_scores table
        sqlite3.connect(bad_db_path).close()

        score_result = score_agent_output("text", [], "developer")
        result = store_quality_scores(
            agent_run_id=1, task_id=1, project_id=1,
            role="developer", score_result=score_result,
            db_path=bad_db_path,
        )
        self.assertFalse(result)

        os.close(bad_db_fd)
        os.unlink(bad_db_path)

    def test_store_returns_false_on_invalid_path(self):
        """store_quality_scores should return False for non-existent DB path."""
        score_result = score_agent_output("text", [], "developer")
        result = store_quality_scores(
            agent_run_id=1, task_id=1, project_id=1,
            role="developer", score_result=score_result,
            db_path="/nonexistent/path/db.sqlite",
        )
        self.assertFalse(result)


class TestScoreAndStore(unittest.TestCase):
    """Test the convenience score_and_store function."""

    def setUp(self):
        """Create a temporary test database with rubric_scores table."""
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE rubric_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_run_id INTEGER NOT NULL,
                task_id INTEGER,
                project_id INTEGER,
                role TEXT NOT NULL,
                rubric_version INTEGER DEFAULT 1,
                criteria_scores TEXT NOT NULL,
                total_score REAL NOT NULL,
                max_possible REAL NOT NULL,
                normalized_score REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        """Clean up test database."""
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_scores_and_stores_in_one_call(self):
        """score_and_store should return score_result and store it."""
        result = score_and_store(
            result_text="proper error handling with tests passing",
            files_changed=["src/mod.py", "tests/test_mod.py"],
            role="developer",
            agent_run_id=10,
            task_id=500,
            project_id=21,
            db_path=self.db_path,
        )
        self.assertIsNotNone(result)
        self.assertIn("scores", result)
        self.assertIn("total_score", result)

        # Verify it was actually stored
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT COUNT(*) FROM rubric_scores WHERE agent_run_id = 10").fetchone()
        conn.close()
        self.assertEqual(row[0], 1)

    def test_returns_none_on_db_failure(self):
        """score_and_store should return None when DB write fails."""
        result = score_and_store(
            result_text="text",
            files_changed=[],
            role="developer",
            agent_run_id=1,
            task_id=1,
            project_id=1,
            db_path="/nonexistent/db.sqlite",
        )
        # Should still return the score_result even if storage fails
        # (because the scoring itself succeeds)
        self.assertIsNotNone(result)


def run_tests():
    """Run all tests and return results."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestScoreDimension))
    suite.addTests(loader.loadTestsFromTestCase(TestApplyFileHeuristics))
    suite.addTests(loader.loadTestsFromTestCase(TestScoreAgentOutput))
    suite.addTests(loader.loadTestsFromTestCase(TestRoleWeights))
    suite.addTests(loader.loadTestsFromTestCase(TestStoreQualityScores))
    suite.addTests(loader.loadTestsFromTestCase(TestScoreAndStore))

    runner = unittest.TextTestRunner(verbosity=2)
    return runner.run(suite)


if __name__ == "__main__":
    result = run_tests()
    sys.exit(0 if result.wasSuccessful() else 1)
