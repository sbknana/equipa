"""Classifier false-positive retry handler.

Migrated from inline guard in equipa/loops.py: the heuristic that turns
a tester-reported failure back into a "proceed" verdict when the
developer demonstrably made file changes (i.e. the failure classifier
fired on a false positive — usually a stale assertion message or a
parse-time hiccup, not a real regression).

Fires on ``dev_test:fail`` and ``diff:empty``. Returns a dict with
``retry=True`` and a ``new_outcome`` suggestion when the classifier
appears to have produced a false positive.

Heuristic (mirrors the prior inline behavior at loops.py around the
"reported failure but made file changes" branch):
    * If dev_result["has_file_changes"] is True AND the tester reported
      a generic failure (no specific assertion mismatch), retry.
    * If diff is empty AND tester reports failure, do NOT retry — that
      is a genuine no-progress case that other guards handle.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Phrases that indicate a real, specific assertion failure (NOT a
# classifier false positive). If present we should NOT retry — the
# developer's code is genuinely broken.
_SPECIFIC_FAILURE_MARKERS: tuple[str, ...] = (
    "assertionerror",
    "assert ",
    "expected ",
    "got ",
    "traceback",
    "stack trace",
    "failed:",
    "fail:",
)


def _has_specific_failure(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(marker in lower for marker in _SPECIFIC_FAILURE_MARKERS)


def check_classifier_false_positive(**kwargs: Any) -> dict[str, Any]:
    """Return ``{"retry": bool, "new_outcome": str|None, "reason": str}``."""
    dev_result = kwargs.get("dev_result") or {}
    tester_result = kwargs.get("tester_result") or {}

    has_file_changes = bool(dev_result.get("has_file_changes"))
    dev_succeeded = bool(dev_result.get("success"))
    tester_text = (
        tester_result.get("result_text")
        or tester_result.get("errors_text")
        or ""
    )

    # Empty-diff branch: caller fired diff:empty. We never recommend a
    # retry for an empty diff — the no-progress detector handles that.
    if kwargs.get("event") == "diff:empty":
        return {
            "retry": False,
            "new_outcome": None,
            "reason": "diff_empty: no false-positive retry recommended",
        }

    if not has_file_changes:
        return {
            "retry": False,
            "new_outcome": None,
            "reason": "no_file_changes: cannot be a classifier false positive",
        }

    if _has_specific_failure(tester_text):
        return {
            "retry": False,
            "new_outcome": None,
            "reason": (
                "specific_failure_detected: tester reported a concrete "
                "assertion / traceback — not a classifier false positive"
            ),
        }

    # Developer changes present, no specific failure marker — looks like
    # a generic-failure false positive. Recommend treating dev as success.
    return {
        "retry": True,
        "new_outcome": "proceed",
        "reason": (
            f"classifier_false_positive: dev success={dev_succeeded} "
            "with file changes; tester failure has no specific marker"
        ),
    }


def register(dispatcher: Any) -> None:
    dispatcher.on("dev_test:fail", check_classifier_false_positive)
    dispatcher.on("diff:empty", check_classifier_false_positive)
