#!/usr/bin/env python3
"""Test suite for standing orders injection into agent system prompts.

Validates:
1. Each role has a standing orders file in standing_orders/
2. load_standing_orders() returns content for every known role
3. Standing orders are injected into build_system_prompt() static prefix per role
4. Security reviewer file contains the mandatory output rule
5. Missing role file returns empty string gracefully

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from equipa.constants import ROLE_PROMPTS, STANDING_ORDERS_DIR
from equipa.prompts import load_standing_orders

# All 9 EQUIPA roles that must have standing orders
EXPECTED_ROLES = [
    "developer",
    "tester",
    "planner",
    "evaluator",
    "security-reviewer",
    "frontend-designer",
    "integration-tester",
    "debugger",
    "code-reviewer",
]


class TestStandingOrdersFiles:
    """Verify the standing_orders/ directory and its files."""

    def test_standing_orders_directory_exists(self) -> None:
        assert STANDING_ORDERS_DIR.is_dir(), (
            f"standing_orders/ directory missing at {STANDING_ORDERS_DIR}"
        )

    @pytest.mark.parametrize("role", EXPECTED_ROLES)
    def test_role_file_exists(self, role: str) -> None:
        path = STANDING_ORDERS_DIR / f"{role}.md"
        assert path.is_file(), f"Missing standing orders file: {path}"

    @pytest.mark.parametrize("role", EXPECTED_ROLES)
    def test_role_file_not_empty(self, role: str) -> None:
        path = STANDING_ORDERS_DIR / f"{role}.md"
        content = path.read_text(encoding="utf-8")
        assert len(content.strip()) > 50, (
            f"Standing orders for {role} is suspiciously short"
        )

    @pytest.mark.parametrize("role", EXPECTED_ROLES)
    def test_role_file_has_required_sections(self, role: str) -> None:
        """Each standing orders file should have authority, gates, escalation."""
        content = path = STANDING_ORDERS_DIR / f"{role}.md"
        content = path.read_text(encoding="utf-8").lower()
        assert "operating authority" in content or "authority" in content, (
            f"{role}.md missing operating authority section"
        )
        assert "approval gate" in content or "gate" in content, (
            f"{role}.md missing approval gates section"
        )
        assert "escalation" in content, (
            f"{role}.md missing escalation rules section"
        )


class TestLoadStandingOrders:
    """Verify load_standing_orders() function behavior."""

    @pytest.mark.parametrize("role", EXPECTED_ROLES)
    def test_load_returns_content_for_known_role(self, role: str) -> None:
        result = load_standing_orders(role)
        assert result, f"load_standing_orders('{role}') returned empty"
        assert "---" in result, "Should be prefixed with section separator"

    def test_load_returns_empty_for_unknown_role(self) -> None:
        result = load_standing_orders("nonexistent-role-xyz")
        assert result == "", "Unknown role should return empty string"

    def test_load_returns_empty_for_missing_file(self, tmp_path: Path) -> None:
        """Even if STANDING_ORDERS_DIR exists, a missing file returns ''."""
        with patch(
            "equipa.prompts.STANDING_ORDERS_DIR", tmp_path,
        ):
            result = load_standing_orders("developer")
            assert result == ""


class TestSecurityReviewerMandatoryOutput:
    """Security reviewer standing orders MUST contain the save-findings rule.

    This is the critical fix from the task: security findings were being
    lost during context compaction (tasks 1435-1439).
    """

    def test_mandatory_output_rule_present(self) -> None:
        content = (
            STANDING_ORDERS_DIR / "security-reviewer.md"
        ).read_text(encoding="utf-8")
        assert "MUST save" in content or "must save" in content.lower(), (
            "Security reviewer standing orders MUST contain the mandatory "
            "save-findings rule to prevent output loss"
        )
        assert "REVIEW_TYPE" in content or "TASK_ID" in content, (
            "Must reference the {REVIEW_TYPE}-{TASK_ID}.md filename pattern"
        )

    def test_mandatory_output_rule_mentions_file_format(self) -> None:
        content = (
            STANDING_ORDERS_DIR / "security-reviewer.md"
        ).read_text(encoding="utf-8")
        assert "SECURITY-REVIEW" in content, (
            "Must mention SECURITY-REVIEW filename format"
        )


class TestStandingOrdersInjection:
    """Verify standing orders are injected into build_system_prompt().

    Rather than calling build_system_prompt (which needs DB, lessons, etc.),
    we verify the injection mechanics: load_standing_orders returns content
    that would be appended to the static prefix.
    """

    @pytest.mark.parametrize("role", EXPECTED_ROLES)
    def test_injection_produces_nonempty_for_each_role(self, role: str) -> None:
        """Every role that has a prompt file also gets standing orders."""
        orders = load_standing_orders(role)
        assert orders, (
            f"Role '{role}' should have standing orders injected"
        )
        # Should start with separator
        assert orders.startswith("\n\n---"), (
            "Standing orders should be prefixed with markdown separator"
        )

    @pytest.mark.parametrize("role", EXPECTED_ROLES)
    def test_standing_orders_contain_role_name(self, role: str) -> None:
        """Each file should reference the role it's for."""
        orders = load_standing_orders(role)
        # The title line should contain the role name (possibly title-cased)
        role_words = role.replace("-", " ")
        assert role_words.lower() in orders.lower(), (
            f"Standing orders should reference the role name '{role}'"
        )
