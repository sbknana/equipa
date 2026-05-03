"""Audit external consumers of the ``equipa`` package public surface.

Walks one or more search roots and finds every import statement that pulls a
name from the top-level ``equipa`` package. The output is the *actual* set of
public symbols that real consumers depend on — the ground truth for what
``equipa/__init__.py`` must expose.

This script exists as evidence behind finding S2 from architecture review #2093:
``equipa/__init__.py`` was previously a 612-line wildcard re-export of 250+
symbols, including private (``_``-prefixed) helpers. Commit a1aa23b trimmed
it to 14 deliberate public entry points. Re-running this script periodically
catches regressions where new external consumers grow the de-facto public
surface beyond what ``__all__`` formally promises.

Usage
-----
    python3 scripts/audit_equipa_imports.py
    python3 scripts/audit_equipa_imports.py /path/to/extra/root ...

The default roots are the EQUIPA repo itself plus its parent share directory
(so sibling projects that import ``equipa`` get audited too). Pass extra paths
as positional arguments to widen the search.

Two patterns are recognised:

* ``from equipa import X, Y, Z``       — explicit symbol imports
* ``from equipa import X as Y``        — aliased symbol imports

Submodule imports (``from equipa.parsing import _extract_section`` or
``import equipa.loops``) are intentionally NOT counted — those bypass
``__init__.py`` and are not part of the top-level public surface contract.

Exit status
-----------
Always 0 unless an unhandled exception occurs. The script is a reporter, not
an enforcer; the regression test suite (``tests/test_public_surface.py``)
owns the actual gate.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_ROOTS: tuple[Path, ...] = (
    Path("/srv/forge-share/AI_Stuff/Equipa-repo"),
    Path("/srv/forge-share/AI_Stuff"),
)

SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".git",
        ".forge-worktrees",
        ".venv",
        "venv",
        "env",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        ".tox",
    }
)


def _iter_python_files(root: Path) -> list[Path]:
    """Yield every ``.py`` file under ``root`` skipping noisy directories."""
    if not root.exists():
        return []
    results: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        results.append(path)
    return results


def _extract_equipa_imports(source: str) -> list[str]:
    """Return every name imported via ``from equipa import ...`` in ``source``.

    Submodule imports (``from equipa.foo import ...`` and ``import equipa.foo``)
    are deliberately excluded — only the top-level package surface counts.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "equipa" and node.level == 0:
                for alias in node.names:
                    if alias.name != "*":
                        names.append(alias.name)
    return names


def audit(roots: list[Path]) -> dict[str, list[Path]]:
    """Collect the consumed public surface across ``roots``.

    Returns a mapping from each consumed symbol name to the list of files
    that import it. Files are reported once per symbol they import.
    """
    consumers: dict[str, list[Path]] = defaultdict(list)
    seen_files: set[Path] = set()

    for root in roots:
        for path in _iter_python_files(root):
            resolved = path.resolve()
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for name in _extract_equipa_imports(source):
                consumers[name].append(path)
    return consumers


def _format_report(consumers: dict[str, list[Path]]) -> str:
    if not consumers:
        return (
            "No `from equipa import X` consumers found.\n"
            "All callers use submodule imports (the recommended pattern).\n"
        )

    lines: list[str] = []
    lines.append(f"Consumed public symbols: {len(consumers)}")
    lines.append("")
    for name in sorted(consumers):
        files = consumers[name]
        lines.append(f"  {name}  ({len(files)} file{'s' if len(files) != 1 else ''})")
        for file_path in files[:5]:
            lines.append(f"      {file_path}")
        if len(files) > 5:
            lines.append(f"      ... and {len(files) - 5} more")
    lines.append("")
    private = sorted(name for name in consumers if name.startswith("_"))
    if private:
        lines.append(f"WARNING: {len(private)} private symbol(s) consumed:")
        for name in private:
            lines.append(f"  {name}")
        lines.append(
            "These should be moved to a stable submodule path or re-homed "
            "in `equipa/legacy.py` before tightening `__all__`."
        )
    else:
        lines.append("OK: no private (_-prefixed) symbols consumed externally.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "roots",
        nargs="*",
        type=Path,
        help="Extra search roots beyond the defaults.",
    )
    args = parser.parse_args(argv)

    roots = list(DEFAULT_ROOTS) + list(args.roots)
    consumers = audit(roots)
    sys.stdout.write(_format_report(consumers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
