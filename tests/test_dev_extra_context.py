"""Regression tests for _build_dev_extra_context.

Covers the compaction-history truncation + paralysis-protection logic
previously inlined in run_dev_test_loop. Critical invariant: paralysis
warnings MUST NOT be truncated and MUST appear before regular history.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

from equipa.loops import _build_dev_extra_context, _split_compaction_history


PARALYSIS_ENTRY = (
    "## CRITICAL: Previous Agent KILLED for Analysis Paralysis\n\n"
    "Do not read first."
)
REGULAR_ENTRY_SHORT = "## Cycle 1 summary\n\nAgent did some work."


class TestSplitCompactionHistory:
    def test_empty(self) -> None:
        assert _split_compaction_history([]) == ([], [])

    def test_segregates_by_marker(self) -> None:
        entries = [REGULAR_ENTRY_SHORT, PARALYSIS_ENTRY, "## Cycle 2 summary"]
        paralysis, regular = _split_compaction_history(entries)
        assert paralysis == [PARALYSIS_ENTRY]
        assert regular == [REGULAR_ENTRY_SHORT, "## Cycle 2 summary"]

    def test_agents_killed_phrase_also_classified_as_paralysis(self) -> None:
        entry = "## EMERGENCY: 3 Agents KILLED on This Task\n\nWrite first."
        paralysis, regular = _split_compaction_history([entry])
        assert paralysis == [entry]
        assert regular == []


class TestBuildDevExtraContextNoFlags:
    def test_returns_empty_when_no_history_no_message(self) -> None:
        assert _build_dev_extra_context([], cycle=1, message_context="", dispatch_config=None) == ""

    def test_anti_compaction_disabled_drops_regular_entries(self) -> None:
        # Without anti_compaction_state, regular entries are silently dropped
        out = _build_dev_extra_context(
            [REGULAR_ENTRY_SHORT], cycle=2, message_context="",
            dispatch_config={"features": {"anti_compaction_state": False}},
        )
        assert out == ""

    def test_anti_compaction_disabled_still_keeps_paralysis(self) -> None:
        out = _build_dev_extra_context(
            [REGULAR_ENTRY_SHORT, PARALYSIS_ENTRY], cycle=2,
            message_context="",
            dispatch_config={"features": {"anti_compaction_state": False}},
        )
        assert PARALYSIS_ENTRY in out
        assert REGULAR_ENTRY_SHORT not in out


class TestBuildDevExtraContextWithFlag:
    flag_on = {"features": {"anti_compaction_state": True}}

    def test_paralysis_appears_before_regular(self) -> None:
        out = _build_dev_extra_context(
            [REGULAR_ENTRY_SHORT, PARALYSIS_ENTRY],
            cycle=1,
            message_context="",
            dispatch_config=self.flag_on,
        )
        assert out.index(PARALYSIS_ENTRY) < out.index(REGULAR_ENTRY_SHORT)

    def test_truncation_at_400_words_only_for_cycle_2_plus_with_2plus_entries(self) -> None:
        long_entry = "word " * 500
        another = "another " * 500
        out = _build_dev_extra_context(
            [long_entry, another], cycle=3, message_context="",
            dispatch_config=self.flag_on,
        )
        assert "[...earlier context trimmed...]" in out
        assert "## Previous Attempts (Cycles 1-2)" in out
        assert len(out.split()) < 500

    def test_no_truncation_with_single_entry(self) -> None:
        long_entry = "word " * 500
        out = _build_dev_extra_context(
            [long_entry], cycle=2, message_context="",
            dispatch_config=self.flag_on,
        )
        assert "[...earlier context trimmed...]" not in out

    def test_paralysis_block_never_truncated(self) -> None:
        # Even when regular entries get truncated, paralysis stays whole.
        long_regular_a = "regwords " * 500
        long_regular_b = "morewords " * 500
        out = _build_dev_extra_context(
            [long_regular_a, long_regular_b, PARALYSIS_ENTRY],
            cycle=3,
            message_context="",
            dispatch_config=self.flag_on,
        )
        assert PARALYSIS_ENTRY in out
        assert "[...earlier context trimmed...]" in out

    def test_message_context_prepended(self) -> None:
        msg = "## Messages from Other Agents\n\nA tester said X."
        out = _build_dev_extra_context(
            [REGULAR_ENTRY_SHORT], cycle=1, message_context=msg,
            dispatch_config=self.flag_on,
        )
        assert out.startswith(msg)

    def test_message_context_only_when_no_history(self) -> None:
        msg = "## Messages from Other Agents\n\nOnly a message."
        out = _build_dev_extra_context(
            [], cycle=1, message_context=msg, dispatch_config=self.flag_on,
        )
        assert out == msg
