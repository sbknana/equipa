"""Allow ``python -m equipa <args>`` invocation.

Mirrors the entry point in ``forge_orchestrator.py`` so the package can be
exercised both as a module (``python -m equipa template ...``) and via the
shim script.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

from equipa.cli import main


if __name__ == "__main__":
    main()
