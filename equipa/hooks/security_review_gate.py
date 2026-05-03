"""Security-reviewer output gate handler.

Migrated from the inline guard in ``equipa/loops.py:run_security_review``
that filters security-reviewer output before persisting findings as
lessons.

Fires on ``security_review:after``. Returns ``{"gate": "open"|"closed",
"findings": list, "reason": str}``. The gate is OPEN when there is at
least one CRITICAL or HIGH finding parsed from the security-reviewer's
result text. When the gate is CLOSED, callers should NOT create lessons
or mark the task as security-blocked — the reviewer either ran with no
findings or its output was empty/unparseable.

Heuristic mirrors ``extract_security_findings`` from loops.py — kept in
this handler to avoid the circular dependency that would arise from
importing loops.py at hook-bootstrap time.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Match severity headers in security-reviewer output, e.g.
#   ## CRITICAL: foo
#   ### HIGH — bar
#   - **CRITICAL**: baz
_SEVERITY_PATTERN = re.compile(
    r"(?im)^\s*(?:\#{1,6}\s*|[-*]\s*)?\**\s*(CRITICAL|HIGH)\s*\**\s*[:\-—]\s*(.+?)\s*$"
)

_GATE_SEVERITIES: frozenset[str] = frozenset({"CRITICAL", "HIGH"})


def extract_findings(text: str) -> list[dict[str, str]]:
    """Parse CRITICAL/HIGH findings from security-reviewer output text."""
    if not text:
        return []
    findings: list[dict[str, str]] = []
    for match in _SEVERITY_PATTERN.finditer(text):
        severity = match.group(1).upper()
        if severity not in _GATE_SEVERITIES:
            continue
        title = match.group(2).strip()
        if not title:
            continue
        findings.append({"severity": severity, "title": title})
    return findings


def check_security_review_output(**kwargs: Any) -> dict[str, Any]:
    """Return gate decision for the security-reviewer output."""
    sec_result = (
        kwargs.get("security_result")
        or kwargs.get("review_result")
        or {}
    )
    text = (
        kwargs.get("result_text")
        or sec_result.get("result_text")
        or ""
    )

    if not text.strip():
        return {
            "gate": "closed",
            "findings": [],
            "reason": "empty_output: security-reviewer produced no text",
        }

    findings = extract_findings(text)
    if not findings:
        return {
            "gate": "closed",
            "findings": [],
            "reason": "no_critical_or_high: reviewer ran cleanly",
        }

    return {
        "gate": "open",
        "findings": findings,
        "reason": (
            f"actionable: {len(findings)} CRITICAL/HIGH finding(s) parsed"
        ),
    }


def register(dispatcher: Any) -> None:
    dispatcher.on("security_review:after", check_security_review_output)
