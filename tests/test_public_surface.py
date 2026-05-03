"""Regression guard for the ``equipa`` package public surface.

Background
----------
``equipa/__init__.py`` was previously a 612-line wildcard re-export of 250+
symbols, including private (``_``-prefixed) helpers from internal modules.
That dissolved every module-boundary benefit of the Phase 1-5 split and made
``import equipa`` eagerly load every submodule.

The fix (commit a1aa23b, S2) trimmed ``__init__.py`` to a small, intentional
public surface declared via ``__all__``. These tests lock that surface in so
future changes cannot accidentally reintroduce wildcard re-exports.

If a NEW symbol genuinely belongs on the public surface, update ``__all__``
in ``equipa/__init__.py`` AND the expected set in ``test_public_api_is_stable``
together — never one without the other.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import equipa


# Hard ceiling for the public surface. The current intentional set is 14
# symbols; we leave a small margin for genuinely-new public entry points.
# If this test fails, the right answer is almost always "remove the new
# export," not "raise the ceiling."
MAX_PUBLIC_SYMBOLS = 30


# The exact set that ``__all__`` is expected to contain. Any drift requires
# a deliberate, reviewed update to both this set and ``equipa/__init__.py``.
EXPECTED_PUBLIC_API: frozenset[str] = frozenset(
    {
        # CLI entry points
        "main",
        "async_main",
        # Top-level loops
        "run_dev_test_loop",
        "run_security_review",
        "run_quality_scoring",
        "run_manager_loop",
        # Dispatch entry points
        "run_auto_dispatch",
        "run_project_dispatch",
        "run_single_goal",
        "run_parallel_goals",
        "run_parallel_tasks",
        # MCP server
        "run_server",
        # Public types
        "PromptResult",
        "LoopDetector",
    }
)


def test_public_api_is_stable() -> None:
    """``equipa.__all__`` must match the reviewed public surface exactly."""
    actual = frozenset(equipa.__all__)
    missing = EXPECTED_PUBLIC_API - actual
    extra = actual - EXPECTED_PUBLIC_API
    assert not missing and not extra, (
        f"Public API drift detected.\n"
        f"  Missing from __all__: {sorted(missing)}\n"
        f"  Unexpectedly added:   {sorted(extra)}\n"
        f"If a new public entry point is intentional, update BOTH "
        f"equipa/__init__.py and EXPECTED_PUBLIC_API in this test."
    )


def test_public_api_below_ceiling() -> None:
    """The public surface must stay below the agreed ceiling."""
    count = len(equipa.__all__)
    assert count <= MAX_PUBLIC_SYMBOLS, (
        f"equipa.__all__ has {count} entries, exceeds ceiling "
        f"of {MAX_PUBLIC_SYMBOLS}. Move new symbols to their submodule and "
        f"import them directly (e.g. `from equipa.parsing import foo`)."
    )


def test_no_private_symbols_re_exported() -> None:
    """No ``_``-prefixed name may appear in ``__all__``.

    Private helpers (``_extract_section``, ``_compute_output_hash`` etc.) must
    be imported from their owning submodule. Re-exporting them from the top
    level disguises module boundaries and freezes them as de-facto public API.
    """
    private = [name for name in equipa.__all__ if name.startswith("_")]
    assert not private, (
        f"Private symbols found in equipa.__all__: {private}. "
        f"Import them from their submodule instead."
    )


def test_all_exports_are_importable() -> None:
    """Every name in ``__all__`` must actually resolve on the package."""
    missing_attrs = [
        name for name in equipa.__all__ if not hasattr(equipa, name)
    ]
    assert not missing_attrs, (
        f"equipa.__all__ lists names not present on the module: "
        f"{missing_attrs}"
    )


def test_init_module_stays_thin() -> None:
    """``equipa/__init__.py`` must remain a thin re-export, not grow back.

    The pre-fix version was 612 lines. We cap at 100 to leave room for the
    module docstring and intentional public re-exports while still failing
    loudly if someone re-introduces wildcard exports or pastes implementation
    code into ``__init__.py``.
    """
    init_path = equipa.__file__
    assert init_path is not None, "equipa package has no __file__"
    with open(init_path, "r", encoding="utf-8") as handle:
        line_count = sum(1 for _ in handle)
    assert line_count <= 100, (
        f"equipa/__init__.py has grown to {line_count} lines. "
        f"It must stay a thin re-export — move implementation into a "
        f"submodule. The pre-S2 version was 612 lines; do not regress."
    )
