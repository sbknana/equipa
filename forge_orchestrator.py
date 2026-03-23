"""Backward-compatibility shim. All implementation lives in equipa/ package.

EQUIPA Phase 5: Multi-Project Orchestration with Resource Allocation

Usage:
    python forge_orchestrator.py --task 63
    python forge_orchestrator.py --task 63 --dev-test
    python forge_orchestrator.py --project 21 --dev-test
    python forge_orchestrator.py --goal "Add a --version flag" --goal-project 21
    python forge_orchestrator.py --parallel-goals goals.json
    python forge_orchestrator.py --auto-run --dry-run
    python forge_orchestrator.py --setup-repos --dry-run

Copyright 2026 Forgeborn
"""

import os

# Force unbuffered output so logs are visible in real-time via nohup/SSH
os.environ["PYTHONUNBUFFERED"] = "1"

from equipa import *  # noqa: F401, F403, E402
from equipa.cli import main, async_main  # noqa: F401, E402

if __name__ == "__main__":
    main()
