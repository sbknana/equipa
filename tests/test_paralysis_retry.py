"""Regression tests for the analysis-paralysis retry helper.

Covers the behavior previously inlined in run_dev_test_loop() after the
S3 decomposition. See ARCHITECTURE-REVIEW-2093 finding S3.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import pytest

from equipa.constants import MAX_DEV_TEST_CYCLES
from equipa.loops import (
    _build_paralysis_injection,
    _handle_paralysis_retry,
    _is_analysis_paralysis,
)


class TestIsAnalysisParalysis:
    @pytest.mark.parametrize(
        "reason",
        [
            "agent killed without file changes",
            "reading instead of writing",
            "analysis paralysis detected",
            "read-only behavior",
            "reading ratio too high",
        ],
    )
    def test_matches_paralysis_phrases(self, reason: str) -> None:
        assert _is_analysis_paralysis(reason) is True

    @pytest.mark.parametrize(
        "reason",
        [
            "timed out",
            "max turns reached",
            "agent failed",
            "",
            "build_broken",
        ],
    )
    def test_rejects_non_paralysis(self, reason: str) -> None:
        assert _is_analysis_paralysis(reason) is False


class TestBuildParalysisInjection:
    def test_first_retry_marks_killed_for_paralysis(self) -> None:
        text = _build_paralysis_injection(paralysis_retries=0, reduced_kill=6)
        assert "KILLED for Analysis Paralysis" in text
        assert "Kill threshold: 6 turns" in text

    def test_second_retry_uses_zero_read_protocol(self) -> None:
        text = _build_paralysis_injection(paralysis_retries=1, reduced_kill=5)
        assert "FINAL CHANCE" in text
        assert "ZERO-READ PROTOCOL" in text
        assert "Kill threshold: 5 turns" in text

    def test_third_retry_uses_emergency_phrasing(self) -> None:
        text = _build_paralysis_injection(paralysis_retries=2, reduced_kill=4)
        assert "EMERGENCY" in text
        assert "3 Agents KILLED" in text
        assert "Kill threshold: 4 turns" in text


class TestHandleParalysisRetry:
    def test_returns_false_for_non_paralysis_reason(self) -> None:
        history: list[str] = []
        config: dict = {}
        result = _handle_paralysis_retry(
            "timed out", cycle=1, compaction_history=history,
            dev_run_config=config,
        )
        assert result is False
        assert history == []
        assert config == {}

    def test_returns_false_on_final_cycle(self) -> None:
        history: list[str] = []
        config: dict = {}
        result = _handle_paralysis_retry(
            "without file changes",
            cycle=MAX_DEV_TEST_CYCLES,
            compaction_history=history,
            dev_run_config=config,
        )
        assert result is False
        assert history == []
        assert "_paralysis_retry_count" not in config

    def test_appends_injection_and_increments_retry_count(self) -> None:
        history: list[str] = []
        config: dict = {}
        result = _handle_paralysis_retry(
            "reading instead of writing",
            cycle=1,
            compaction_history=history,
            dev_run_config=config,
        )
        assert result is True
        assert len(history) == 1
        assert "KILLED for Analysis Paralysis" in history[0]
        assert config["_paralysis_retry_count"] == 1

    def test_escalates_on_repeated_paralysis(self) -> None:
        # Seed history with one prior paralysis kill marker
        history: list[str] = [
            "## CRITICAL: Previous Agent KILLED for Analysis Paralysis\n..."
        ]
        config: dict = {"_paralysis_retry_count": 1}

        result = _handle_paralysis_retry(
            "analysis paralysis",
            cycle=2,
            compaction_history=history,
            dev_run_config=config,
        )
        assert result is True
        # Now there should be 2 paralysis entries; the new one is the
        # "FINAL CHANCE" / 2-agents-killed phrasing.
        assert len(history) == 2
        assert "FINAL CHANCE" in history[1]
        assert config["_paralysis_retry_count"] == 2
