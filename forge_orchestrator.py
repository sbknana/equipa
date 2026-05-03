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

# Load .env BEFORE importing equipa — constants.py reads os.environ at
# import time, so the API key must be in the environment before that happens.
# This fixes ANTHROPIC_API_KEY being unavailable in nohup/background processes
# that don't source ~/.bashrc.
from equipa.env_loader import load_dotenv  # noqa: E402
load_dotenv()

from equipa.cli import main, async_main  # noqa: F401, E402

# Backward-compatibility re-exports for legacy test imports. New code should
# import these directly from their submodules. These names are NOT part of the
# stable `equipa` package public surface (see equipa/__init__.py) — they exist
# here solely so that `tests/` and any pre-Phase-5 tooling continue to work.
from equipa.constants import (  # noqa: F401, E402
    BUDGET_CHECK_INTERVAL,
    BUDGET_CRITICAL_THRESHOLD,
    BUDGET_HALFWAY_THRESHOLD,
    COST_ESTIMATE_PER_TURN,
    COST_LIMITS,
    EARLY_TERM_EXEMPT_ROLES,
    EARLY_TERM_FINAL_WARN_TURNS,
    EARLY_TERM_KILL_TURNS,
    EARLY_TERM_STUCK_PHRASES,
    EARLY_TERM_WARN_TURNS,
    MONOLOGUE_EXEMPT_TURNS,
    MONOLOGUE_THRESHOLD,
    PREFLIGHT_SKIP_KEYWORDS,
    PREFLIGHT_TIMEOUT,
    SKILL_MANIFEST_FILE,
    THEFORGE_DB,
)
from equipa.db import (  # noqa: F401, E402
    bulk_log_agent_actions,
    classify_error,
    ensure_schema,
    log_agent_action,
)
from equipa.dispatch import (  # noqa: F401, E402
    DEFAULT_FEATURE_FLAGS,
    is_feature_enabled,
    load_dispatch_config,
)
from equipa.git_ops import detect_project_language  # noqa: F401, E402
from equipa.lessons import (  # noqa: F401, E402
    _injected_episodes_by_task,
    format_episodes_for_injection,
    format_lessons_for_injection,
    get_relevant_episodes,
    update_episode_injection_count,
    update_episode_q_values,
    update_injected_episode_q_values_for_task,
    update_lesson_injection_count,
)
from equipa.monitoring import (  # noqa: F401, E402
    LOOP_TERMINATE_THRESHOLD,
    LOOP_WARNING_THRESHOLD,
    LoopDetector,
    _check_cost_limit,
    _check_git_changes,
    _check_monologue,
    _check_stuck_phrases,
    _compute_output_hash,
    _detect_tool_loop,
    _get_budget_message,
    _parse_early_complete,
)
from equipa.messages import (  # noqa: F401, E402
    format_messages_for_prompt,
    mark_messages_read,
    post_agent_message,
    read_agent_messages,
)
from equipa.preflight import preflight_build_check  # noqa: F401, E402
from equipa.prompts import build_system_prompt  # noqa: F401, E402
from equipa.security import (  # noqa: F401, E402
    generate_skill_manifest,
    verify_skill_integrity,
    write_skill_manifest,
)

if __name__ == "__main__":
    main()
