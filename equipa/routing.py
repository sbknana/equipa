"""Intelligent task routing — complexity scoring + model selection + circuit breaker.

Layer 2 module: imports from equipa.constants only.
Complexity scoring uses 4 weighted features to classify tasks as haiku/sonnet/opus tier.
Circuit breaker tracks consecutive failures per model with 60s recovery window.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import re
import time
from typing import Any

from equipa.constants import DEFAULT_MODEL

# --- Complexity Scoring Keywords ---

# HIGH complexity: architectural, security, distributed system work
HIGH_KEYWORDS = frozenset({
    "architect", "security", "refactor", "distributed", "migrate",
    "optimize", "performance", "scalability", "infrastructure",
    "authentication", "authorization", "encryption", "vulnerability",
    "concurrent", "parallel", "multi-threaded", "race condition",
    "database migration", "schema design", "api design",
})

# MEDIUM complexity: standard feature/fix work
MEDIUM_KEYWORDS = frozenset({
    "implement", "feature", "fix", "test", "endpoint", "integration",
    "component", "validation", "error handling", "logging",
    "configuration", "deployment", "monitoring", "caching",
    "query", "model", "controller", "service", "middleware",
})

# LOW complexity: trivial edits
LOW_KEYWORDS = frozenset({
    "typo", "comment", "format", "rename", "style", "whitespace",
    "import", "dependency", "version", "update package", "bump",
    "documentation", "readme", "spelling", "punctuation",
})

# --- Feature Weights ---

WEIGHT_LEXICAL = 0.2
WEIGHT_SEMANTIC = 0.35
WEIGHT_SCOPE = 0.25
WEIGHT_UNCERTAINTY = 0.2

# --- Model Selection Thresholds ---

THRESHOLD_HAIKU = 0.3   # < 0.3 = haiku
THRESHOLD_SONNET = 0.6  # 0.3-0.6 = sonnet, >= 0.6 = opus

# --- Circuit Breaker Settings ---

CB_FAILURE_THRESHOLD = 5  # consecutive failures before circuit opens
CB_RECOVERY_SECONDS = 60  # time before attempting recovery
CB_STATE_CLOSED = "closed"
CB_STATE_OPEN = "open"
CB_STATE_HALF_OPEN = "half_open"

# In-memory circuit breaker state per model
_circuit_breaker_state: dict[str, dict[str, Any]] = {}


def _lexical_complexity(text: str) -> float:
    """Compute lexical complexity: avg word length + avg sentence length.

    Returns normalized score 0.0-1.0 (higher = more complex).
    """
    if not text.strip():
        return 0.0

    words = text.split()
    if not words:
        return 0.0

    # Average word length (normalize: 4 chars = 0.4, 10+ chars = 1.0)
    avg_word_len = sum(len(w) for w in words) / len(words)
    word_score = min(avg_word_len / 10.0, 1.0)

    # Average sentence length (normalize: 10 words = 0.5, 30+ words = 1.0)
    sentences = [s.strip() for s in re.split(r"[.!?]+", text) if s.strip()]
    if sentences:
        avg_sent_len = len(words) / len(sentences)
        sent_score = min(avg_sent_len / 30.0, 1.0)
    else:
        sent_score = 0.5

    return (word_score + sent_score) / 2.0


def _semantic_depth(text: str) -> float:
    """Compute semantic depth via keyword matching.

    Returns weighted score 0.0-1.0 based on HIGH/MEDIUM/LOW keyword presence.
    """
    text_lower = text.lower()

    high_count = sum(1 for kw in HIGH_KEYWORDS if kw in text_lower)
    medium_count = sum(1 for kw in MEDIUM_KEYWORDS if kw in text_lower)
    low_count = sum(1 for kw in LOW_KEYWORDS if kw in text_lower)

    # Weighted scoring: HIGH=1.0, MEDIUM=0.5, LOW=0.1
    total_matches = high_count + medium_count + low_count
    if total_matches == 0:
        return 0.5  # neutral default

    weighted_score = (
        (high_count * 1.0) + (medium_count * 0.5) + (low_count * 0.1)
    ) / total_matches

    return min(weighted_score, 1.0)


def _task_scope(text: str) -> float:
    """Compute task scope via regex patterns for multi-file/system-wide work.

    Returns score 0.0-1.0 (higher = broader scope).
    """
    text_lower = text.lower()
    score = 0.0

    # Multi-file indicators
    multi_file_patterns = [
        r"\bmultiple\s+files\b",
        r"\ball\s+files\b",
        r"\bproject-wide\b",
        r"\bcodebase\b",
        r"\bentire\s+system\b",
        r"\bacross\s+\d+\s+files\b",
    ]
    if any(re.search(p, text_lower) for p in multi_file_patterns):
        score += 0.5

    # System-wide indicators
    system_patterns = [
        r"\barchitecture\b",
        r"\binfrastructure\b",
        r"\bmigration\b",
        r"\bdeployment\b",
        r"\bCI/CD\b",
        r"\bpipeline\b",
    ]
    if any(re.search(p, text_lower) for p in system_patterns):
        score += 0.5

    return min(score, 1.0)


def _uncertainty_level(text: str) -> float:
    """Compute uncertainty level via debug/investigate/unclear patterns.

    Returns score 0.0-1.0 (higher = more uncertain).
    """
    text_lower = text.lower()
    score = 0.0

    uncertainty_patterns = [
        r"\bdebug\b",
        r"\binvestigate\b",
        r"\bdiagnose\b",
        r"\bnot sure\b",
        r"\bunclear\b",
        r"\bfind out\b",
        r"\broot cause\b",
        r"\bwhy\b.*\bfailing\b",
        r"\bintermittent\b",
    ]

    match_count = sum(1 for p in uncertainty_patterns if re.search(p, text_lower))
    score = min(match_count / len(uncertainty_patterns), 1.0)

    return score


def score_complexity(description: str, title: str = "") -> float:
    """Score task complexity using 4 weighted features.

    Args:
        description: Task description text
        title: Optional task title

    Returns:
        Complexity score 0.0-1.0 (0=trivial, 1=highly complex)
    """
    combined = f"{title} {description}".strip()
    if not combined:
        return 0.5  # neutral default

    lexical = _lexical_complexity(combined)
    semantic = _semantic_depth(combined)
    scope = _task_scope(combined)
    uncertainty = _uncertainty_level(combined)

    score = (
        WEIGHT_LEXICAL * lexical
        + WEIGHT_SEMANTIC * semantic
        + WEIGHT_SCOPE * scope
        + WEIGHT_UNCERTAINTY * uncertainty
    )

    return round(score, 3)


def select_model_by_complexity(
    score: float,
    uncertainty: float,
    config: dict[str, Any] | None = None,
) -> str:
    """Select model tier based on complexity score and uncertainty.

    Args:
        score: Complexity score from score_complexity()
        uncertainty: Uncertainty level from _uncertainty_level()
        config: Optional dispatch config with model overrides

    Returns:
        Model name: "haiku", "sonnet", or "opus"
    """
    # Uncertainty escalation: >0.15 auto-bumps tier
    if uncertainty > 0.15:
        score = min(score + 0.2, 1.0)

    # Three-tier thresholds
    if score < THRESHOLD_HAIKU:
        model = "haiku"
    elif score < THRESHOLD_SONNET:
        model = "sonnet"
    else:
        model = "opus"

    # Check for config overrides
    if config and "model_overrides" in config:
        overrides = config["model_overrides"]
        if model in overrides:
            model = overrides[model]

    return model


def record_model_outcome(model: str, success: bool) -> None:
    """Record success/failure outcome for circuit breaker tracking.

    Args:
        model: Model name
        success: True if task succeeded, False if failed
    """
    if model not in _circuit_breaker_state:
        _circuit_breaker_state[model] = {
            "state": CB_STATE_CLOSED,
            "consecutive_failures": 0,
            "last_failure_time": 0.0,
        }

    state = _circuit_breaker_state[model]

    if success:
        # Reset on success
        state["consecutive_failures"] = 0
        if state["state"] == CB_STATE_HALF_OPEN:
            state["state"] = CB_STATE_CLOSED
    else:
        # Increment failure count
        state["consecutive_failures"] += 1
        state["last_failure_time"] = time.time()

        # Open circuit if threshold exceeded
        if state["consecutive_failures"] >= CB_FAILURE_THRESHOLD:
            state["state"] = CB_STATE_OPEN


def _get_circuit_state(model: str) -> str:
    """Get current circuit breaker state for model.

    Handles recovery window logic: OPEN -> HALF_OPEN after recovery time.

    Args:
        model: Model name

    Returns:
        Circuit state: "closed", "open", or "half_open"
    """
    if model not in _circuit_breaker_state:
        return CB_STATE_CLOSED

    state = _circuit_breaker_state[model]
    current_time = time.time()

    # Check recovery window
    if state["state"] == CB_STATE_OPEN:
        elapsed = current_time - state["last_failure_time"]
        if elapsed >= CB_RECOVERY_SECONDS:
            state["state"] = CB_STATE_HALF_OPEN
            state["consecutive_failures"] = 0

    return state["state"]


def auto_select_model(
    task: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> str:
    """Auto-select model for task using complexity scoring + circuit breaker.

    Args:
        task: Task dict with "description" and optional "title" keys
        config: Optional dispatch config

    Returns:
        Selected model name (with fallback if circuit is open)
    """
    description = task.get("description", "")
    title = task.get("title", "")

    # Score complexity
    complexity = score_complexity(description, title)
    uncertainty = _uncertainty_level(f"{title} {description}")

    # Select model tier
    model = select_model_by_complexity(complexity, uncertainty, config)

    # Check circuit breaker
    circuit_state = _get_circuit_state(model)

    if circuit_state == CB_STATE_OPEN:
        # Circuit open: fallback to next tier up
        fallback_map = {"haiku": "sonnet", "sonnet": "opus", "opus": "opus"}
        model = fallback_map.get(model, DEFAULT_MODEL)

    return model
