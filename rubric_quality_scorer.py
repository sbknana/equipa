"""
Post-task quality scorer using architecture-review patterns.

Scores agent output on 5 quality dimensions by pattern-matching on the
agent's result_text. Stores scores in the rubric_scores table for
ForgeSmith analysis and rubric evolution.

Each dimension is scored 0-10:
  - error_handling: Evidence of proper error handling in the agent's work
  - naming_consistency: Evidence of clean naming and code style
  - code_structure: Evidence of well-structured, modular code
  - test_coverage: Evidence that tests were written or considered
  - documentation: Evidence of documentation, comments, or clear output

Copyright 2026 Forgeborn
"""

import json
import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Quality dimensions and their scoring indicators.
# Each dimension has positive indicators (evidence of quality) and
# negative indicators (evidence of problems). Scores start at 5 (neutral)
# and adjust up/down based on matches.
QUALITY_DIMENSIONS = {
    "error_handling": {
        "positive": [
            (r"error handling", 2),
            (r"try[\s/](?:except|catch|finally)", 2),
            (r"exception|raise|throw", 1),
            (r"graceful(?:ly)?", 1),
            (r"validate|validation|sanitize", 1),
            (r"edge case", 1),
            (r"boundary check", 1),
            (r"input validation", 2),
            (r"error recover", 1),
            (r"fallback", 1),
        ],
        "negative": [
            (r"swallow(?:ed|ing)? (?:error|exception)", -3),
            (r"bare except", -2),
            (r"except\s*:", -2),
            (r"no error handling", -3),
            (r"missing error", -2),
            (r"unhandled exception", -2),
            (r"silent(?:ly)? fail", -2),
            (r"crash(?:es|ed|ing)?", -1),
            (r"ignor(?:e|ed|ing) error", -2),
        ],
    },
    "naming_consistency": {
        "positive": [
            (r"consistent naming", 2),
            (r"clean (?:code|naming|style)", 2),
            (r"follow(?:s|ed|ing) (?:convention|pattern|style)", 2),
            (r"snake_case|camelCase|PascalCase", 1),
            (r"descriptive (?:name|variable|function)", 2),
            (r"meaningful name", 2),
            (r"refactor(?:ed|ing)? (?:name|variable)", 1),
            (r"renamed? (?:for|to) clarity", 2),
        ],
        "negative": [
            (r"inconsistent naming", -3),
            (r"poor naming", -3),
            (r"single.letter variable", -2),
            (r"(?:unclear|confusing|ambiguous) name", -2),
            (r"magic (?:number|string|value)", -2),
            (r"hardcoded", -1),
            (r"naming (?:issue|problem|violation)", -2),
        ],
    },
    "code_structure": {
        "positive": [
            (r"modular", 2),
            (r"well.structured", 2),
            (r"clean architecture", 2),
            (r"separation of concerns", 2),
            (r"single responsibility", 2),
            (r"refactor(?:ed|ing)?", 1),
            (r"extracted? (?:function|method|class|module)", 2),
            (r"DRY|don.t repeat", 1),
            (r"decoupl(?:e|ed|ing)", 1),
            (r"abstraction", 1),
            (r"interface|protocol", 1),
        ],
        "negative": [
            (r"spaghetti", -3),
            (r"tightly coupled", -2),
            (r"god (?:class|function|object)", -3),
            (r"duplicat(?:e|ed|ion)", -2),
            (r"copy.paste", -2),
            (r"monolithic", -1),
            (r"too (?:long|large|complex)", -2),
            (r"nested (?:too|deeply)", -2),
            (r"circular (?:dependency|import)", -3),
        ],
    },
    "test_coverage": {
        "positive": [
            (r"test(?:s|ed|ing)?[\s_](?:pass|writ|add|creat|cover)", 2),
            (r"(?:unit|integration|e2e) test", 2),
            (r"test (?:suite|file|case)", 2),
            (r"assert(?:ion|s|Equal|True|Raises)", 1),
            (r"100% (?:pass|coverage)", 3),
            (r"all tests pass", 3),
            (r"\d+ tests? pass", 2),
            (r"edge case test", 2),
            (r"test_\w+\.(?:py|ts|js|go)", 2),
            (r"spec\.(?:ts|js)", 1),
            (r"coverage", 1),
        ],
        "negative": [
            (r"no tests?", -3),
            (r"test(?:s)? fail", -2),
            (r"missing test", -3),
            (r"skip(?:ped|ping)? test", -1),
            (r"untested", -2),
            (r"without test", -2),
            (r"test(?:s)? broken", -2),
            (r"0 tests", -3),
        ],
    },
    "documentation": {
        "positive": [
            (r"docstring", 2),
            (r"comment(?:s|ed)?", 1),
            (r"document(?:ed|ation|ing)", 2),
            (r"README", 1),
            (r"type (?:hint|annotation)", 2),
            (r"JSDoc|pydoc|godoc", 2),
            (r"SUMMARY:", 1),
            (r"REFLECTION:", 1),
            (r"DECISIONS:", 1),
            (r"explain(?:ed|ing|s)?", 1),
            (r"well.documented", 2),
        ],
        "negative": [
            (r"no (?:comment|doc|documentation)", -2),
            (r"undocumented", -2),
            (r"missing (?:comment|doc|documentation)", -2),
            (r"unclear (?:purpose|intent)", -2),
        ],
    },
}

# Role-specific weight multipliers. Some dimensions matter more for
# certain roles. A weight of 1.0 is neutral; >1.0 amplifies, <1.0 dampens.
ROLE_WEIGHTS = {
    "developer": {
        "error_handling": 1.2,
        "naming_consistency": 1.0,
        "code_structure": 1.2,
        "test_coverage": 1.0,
        "documentation": 0.8,
    },
    "tester": {
        "error_handling": 0.8,
        "naming_consistency": 0.8,
        "code_structure": 0.8,
        "test_coverage": 1.5,
        "documentation": 0.8,
    },
    "code-reviewer": {
        "error_handling": 1.0,
        "naming_consistency": 1.2,
        "code_structure": 1.2,
        "test_coverage": 0.8,
        "documentation": 1.2,
    },
    "security-reviewer": {
        "error_handling": 1.5,
        "naming_consistency": 0.8,
        "code_structure": 1.0,
        "test_coverage": 0.8,
        "documentation": 1.0,
    },
}

# Default weights for roles not explicitly listed
_DEFAULT_WEIGHTS = {
    "error_handling": 1.0,
    "naming_consistency": 1.0,
    "code_structure": 1.0,
    "test_coverage": 1.0,
    "documentation": 1.0,
}


def _score_dimension(text, dimension_name):
    """Score a single quality dimension by pattern matching.

    Starts at a baseline of 5 and adjusts based on matched indicators.
    Clamps the final score to [0, 10].

    Args:
        text: The agent result_text to analyze (lowercased).
        dimension_name: Key into QUALITY_DIMENSIONS.

    Returns:
        Integer score in range [0, 10].
    """
    config = QUALITY_DIMENSIONS.get(dimension_name)
    if not config:
        return 5  # Unknown dimension, return neutral

    score = 5  # Neutral baseline

    for pattern, delta in config["positive"]:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            # Cap each pattern's contribution to avoid a single repeated
            # keyword dominating the score.
            score += delta * min(len(matches), 3)

    for pattern, delta in config["negative"]:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            score += delta * min(len(matches), 3)

    return max(0, min(10, score))


def _apply_file_heuristics(scores, files_changed):
    """Adjust scores based on the FILES_CHANGED list.

    Provides additional signal beyond text analysis. For example, if test
    files appear in files_changed, boost test_coverage. If no files were
    changed, reduce code_structure.

    Args:
        scores: Dict of dimension -> score (mutated in place).
        files_changed: List of file paths changed by the agent.
    """
    if not files_changed:
        # No files changed — reduce structural scores
        scores["code_structure"] = max(0, scores["code_structure"] - 2)
        scores["test_coverage"] = max(0, scores["test_coverage"] - 2)
        return

    test_files = [
        f for f in files_changed
        if re.search(r"test[_\-.]|spec\.|_test\.", f, re.IGNORECASE)
    ]
    if test_files:
        scores["test_coverage"] = min(10, scores["test_coverage"] + 2)

    doc_files = [
        f for f in files_changed
        if re.search(r"readme|\.md$|docs[/\\]|changelog", f, re.IGNORECASE)
    ]
    if doc_files:
        scores["documentation"] = min(10, scores["documentation"] + 1)

    # Many files changed suggests structured work (unless it's too many)
    file_count = len(files_changed)
    if 2 <= file_count <= 10:
        scores["code_structure"] = min(10, scores["code_structure"] + 1)
    elif file_count > 20:
        # Suspiciously large changeset — might indicate unfocused work
        scores["code_structure"] = max(0, scores["code_structure"] - 1)


def score_agent_output(result_text, files_changed, role):
    """Score an agent's output on 5 quality dimensions.

    Analyzes the agent's result_text using pattern matching to detect
    indicators of code quality across error handling, naming, structure,
    testing, and documentation.

    Args:
        result_text: The full text output from the agent (RESULT block, etc.).
        files_changed: List of file paths the agent created or modified.
        role: The agent's role (e.g., "developer", "tester").

    Returns:
        Dict with keys:
          - "scores": Dict mapping dimension name to integer score [0-10]
          - "total_score": Sum of all dimension scores (float, after weighting)
          - "max_possible": Maximum achievable score (always 50.0)
          - "normalized_score": total_score / max_possible, in [0.0, 1.0]
          - "details": Dict mapping dimension to list of matched indicators
    """
    if not result_text:
        result_text = ""

    text_lower = result_text.lower()
    files_changed = files_changed or []

    # Score each dimension
    raw_scores = {}
    details = {}
    for dimension in QUALITY_DIMENSIONS:
        raw_scores[dimension] = _score_dimension(text_lower, dimension)

        # Collect matched indicators for transparency
        matched = []
        for pattern, delta in QUALITY_DIMENSIONS[dimension]["positive"]:
            if re.search(pattern, text_lower, re.IGNORECASE):
                matched.append(f"+{delta}: {pattern}")
        for pattern, delta in QUALITY_DIMENSIONS[dimension]["negative"]:
            if re.search(pattern, text_lower, re.IGNORECASE):
                matched.append(f"{delta}: {pattern}")
        if matched:
            details[dimension] = matched

    # Apply file-based heuristics
    _apply_file_heuristics(raw_scores, files_changed)

    # Apply role-specific weights and clamp to [0, 10]
    role_weights = ROLE_WEIGHTS.get(role, _DEFAULT_WEIGHTS)
    weighted_scores = {}
    for dimension, raw_score in raw_scores.items():
        weight = role_weights.get(dimension, 1.0)
        weighted_scores[dimension] = min(10.0, max(0.0, round(raw_score * weight, 1)))

    total_score = round(sum(weighted_scores.values()), 1)
    max_possible = 50.0  # 5 dimensions * 10 max each
    normalized = round(total_score / max_possible, 3) if max_possible > 0 else 0.0

    return {
        "scores": weighted_scores,
        "total_score": total_score,
        "max_possible": max_possible,
        "normalized_score": normalized,
        "details": details,
    }


def store_quality_scores(agent_run_id, task_id, project_id, role, score_result,
                         db_path=None):
    """Store quality scores in the rubric_scores table.

    Uses the existing rubric_scores table schema. The criteria_scores field
    stores the per-dimension scores as JSON.

    Args:
        agent_run_id: The agent_runs.id for this run.
        task_id: The task ID.
        project_id: The project ID.
        role: Agent role string.
        score_result: Dict returned by score_agent_output().
        db_path: Optional explicit database path. If None, uses THEFORGE_DB
                 environment variable or default path.

    Returns:
        True if stored successfully, False otherwise.
    """
    try:
        if db_path is None:
            import os
            db_path = os.environ.get(
                "THEFORGE_DB",
                str(Path(__file__).parent / "theforge.db"),
            )

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """INSERT INTO rubric_scores
               (agent_run_id, task_id, project_id, role, rubric_version,
                criteria_scores, total_score, max_possible, normalized_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent_run_id,
                task_id,
                project_id,
                role,
                1,  # rubric_version — quality scorer v1
                json.dumps(score_result["scores"]),
                score_result["total_score"],
                score_result["max_possible"],
                score_result["normalized_score"],
            ),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.warning("Failed to store quality scores: %s", e)
        return False


def score_and_store(result_text, files_changed, role, agent_run_id,
                    task_id, project_id, db_path=None):
    """Convenience: score agent output and store results in one call.

    Args:
        result_text: Agent's full output text.
        files_changed: List of file paths changed.
        role: Agent role string.
        agent_run_id: The agent_runs.id.
        task_id: The task ID.
        project_id: The project ID.
        db_path: Optional database path override.

    Returns:
        The score_result dict from score_agent_output(), or None on failure.
    """
    try:
        score_result = score_agent_output(result_text, files_changed, role)
        stored = store_quality_scores(
            agent_run_id, task_id, project_id, role, score_result,
            db_path=db_path,
        )
        if stored:
            logger.info(
                "Quality score for run %d: %.1f/%.1f (%.1f%%)",
                agent_run_id,
                score_result["total_score"],
                score_result["max_possible"],
                score_result["normalized_score"] * 100,
            )
        return score_result
    except Exception as e:
        logger.warning("Quality scoring failed for run %d: %s", agent_run_id, e)
        return None
