"""Vacuous-pass guard handler.

Migrated from inline guard logic in equipa/loops.py. Detects the
"vacuous pass" failure mode where a tester reports success but the
developer made no real file changes (or only touched no-op files such
as the local .forge-state.json checkpoint).

Fires on ``dev_test:fail`` and ``attempt:after``. Returns a dict with
``vacuous=True`` when the heuristic trips so callers can mark the
attempt as not-truly-passed and trigger a retry with stricter prompts.

Heuristic (mirrors the prior inline behavior):
    * If the tester result reports zero tests run AND there are no real
      file changes (FILES_CHANGED is empty or only contains the state
      file), classify as vacuous.
    * If FILES_CHANGED contains only documentation files (*.md) but the
      task description verb is "implement"/"add"/"fix", also vacuous.

The handler is pure — it only inspects the event payload and returns a
classification dict. It MUST NOT mutate orchestrator state. Callers in
loops.py read the returned dict from ``fire_async`` results and react
accordingly.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Files that don't count as real work.
_TRIVIAL_FILES: frozenset[str] = frozenset({
    ".forge-state.json",
    ".forge_state.json",
})

# Verbs in a task description that REQUIRE code changes (not just docs).
_CODE_CHANGE_VERBS: tuple[str, ...] = (
    "implement", "add", "fix", "create", "build",
    "refactor", "migrate", "update", "rename", "remove",
)


def _is_trivial_change(files_changed: list[str]) -> bool:
    """True if the change list is empty or only touches sentinel files."""
    if not files_changed:
        return True
    real = [f for f in files_changed if f.strip() and f.strip() not in _TRIVIAL_FILES]
    return len(real) == 0


def _is_docs_only(files_changed: list[str]) -> bool:
    """True if every changed file is a markdown / docs file."""
    real = [f.strip() for f in files_changed if f.strip()]
    if not real:
        return False
    return all(f.lower().endswith((".md", ".rst", ".txt")) for f in real)


def check_vacuous_pass(**kwargs: Any) -> dict[str, Any]:
    """Return ``{"vacuous": bool, "reason": str}`` for the current attempt."""
    tester_result = kwargs.get("tester_result") or {}
    dev_result = kwargs.get("dev_result") or {}
    task = kwargs.get("task") or {}

    files_changed_raw = (
        dev_result.get("files_changed")
        or kwargs.get("files_changed")
        or []
    )
    if isinstance(files_changed_raw, str):
        files_changed = [
            line.strip(" -*\t")
            for line in files_changed_raw.splitlines()
            if line.strip()
        ]
    else:
        files_changed = list(files_changed_raw)

    tests_run = int(tester_result.get("tests_run") or 0)
    tester_outcome = (tester_result.get("result") or "").lower()

    # Case 1: tester reports a "pass" but nothing meaningful was changed.
    if tester_outcome in {"pass", "no-tests"} and _is_trivial_change(files_changed):
        return {
            "vacuous": True,
            "reason": (
                f"vacuous_pass: tester={tester_outcome} tests_run={tests_run} "
                f"files_changed={files_changed!r}"
            ),
        }

    # Case 2: code-change verb in task description, but only docs touched.
    description = (task.get("description") or task.get("title") or "").lower()
    if any(verb in description for verb in _CODE_CHANGE_VERBS):
        if _is_docs_only(files_changed):
            return {
                "vacuous": True,
                "reason": (
                    "vacuous_pass: task description requires code changes "
                    f"but only documentation was touched ({files_changed!r})"
                ),
            }

    return {"vacuous": False, "reason": ""}


def register(dispatcher: Any) -> None:
    """Register the vacuous-pass callbacks on the colon-namespaced events."""
    dispatcher.on("dev_test:fail", check_vacuous_pass)
    dispatcher.on("attempt:after", check_vacuous_pass)
