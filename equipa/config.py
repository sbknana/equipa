"""EQUIPA configuration module — feature flags and dispatch config loading.

Layer-2 module: depends only on stdlib and equipa.constants. Holds the
read-only feature-flag and dispatch-config primitives that previously lived
in equipa.dispatch. dispatch.py imports loops.py at module load, so any
caller that needed is_feature_enabled / load_dispatch_config from inside
loops.py was forced to do an inline `from equipa.dispatch import ...` to
dodge the cycle. Hosting these primitives here breaks that cycle: both
dispatch.py and loops.py import from equipa.config, which imports
nothing from either of them.

Exports:
    DEFAULT_FEATURE_FLAGS
    DEFAULT_DISPATCH_CONFIG
    is_feature_enabled
    load_dispatch_config
    set_active_dispatch_config
    get_active_dispatch_config
    get_configured_model
    get_approved_model_upgrades
    is_model_allowed
    DOWNGRADE_MODEL_FAMILIES
    is_downgrade_model
    resolve_claude_model
    get_persistent_retry_max_attempts

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from equipa.constants import DEFAULT_MODEL, THEFORGE_DB


DEFAULT_FEATURE_FLAGS: dict[str, bool] = {
    "language_prompts": True,
    "hooks": False,
    "mcp_health": False,
    "forgesmith_lessons": True,
    "forgesmith_episodes": True,
    "gepa_ab_testing": False,
    "security_review": True,
    # When True, agent_runner injects a Claude Code PreToolUse hook into the
    # spawned CLI subprocess (via a generated --settings file) so that
    # equipa.bash_security.check_bash_command runs BEFORE the Bash tool
    # executes — genuine pre-execution blocking, not the reactive
    # detect-and-terminate that the stream observer provides. DEFAULT FALSE:
    # the reactive stream check (agent_runner.py, defense-in-depth) is always
    # on; this flag only adds the true pre-execution gate on top. See task 2703.
    "bash_security_pretooluse": False,
    # When True (default), a missing SECURITY-REVIEW-NNNN.md artifact after
    # the security-review agent runs is treated as a gate-blocking failure
    # (fail-closed). Set to False only if your workflow accepts the
    # pre-2341 trade-off: a crashed/silenced reviewer silently downgrades
    # to "no findings" and the branch is allowed to merge. See task 2341.
    "security_review_block_on_missing_artifact": True,
    "quality_scoring": True,
    "anti_compaction_state": True,
    "vector_memory": False,
    "auto_model_routing": False,
    "knowledge_graph": False,
    "autoresearch": True,
    "rlm_decompose": False,
    "config_versioning": False,
    "session_persistence": False,
    "project_templates": False,
    # When True (default), ensure_schema also applies schema_personal.sql — the
    # owner-only personal-PM data model (competitors, content_tickler, reminders,
    # voice_messages, etc.). The owner's prod install keeps this on so pulling the
    # split touches nothing. Public/fresh installs set it False in dispatch_config
    # to ship the orchestrator product WITHOUT the personal-PM tables. Purely
    # additive either way — schema_personal.sql is CREATE ... IF NOT EXISTS only.
    "personal_pm_tables": True,
}

DEFAULT_DISPATCH_CONFIG: dict = {
    "max_concurrent": 8,
    "model": DEFAULT_MODEL,
    "max_turns": 25,
    "max_tasks_per_project": 3,
    "skip_projects": [],
    "priority_boost": {},
    "only_projects": [],
    "security_review": False,
    "features": dict(DEFAULT_FEATURE_FLAGS),
    "autoresearch_max_retries": 3,
}


def is_feature_enabled(dispatch_config: dict | None, feature_name: str) -> bool:
    """Check if a feature flag is enabled.

    Reads from dispatch_config["features"][feature_name]. Falls back to
    DEFAULT_FEATURE_FLAGS if the feature is not in the config.

    Returns True/False. Unknown features default to False.
    """
    if dispatch_config is None:
        return DEFAULT_FEATURE_FLAGS.get(feature_name, False)
    features = dispatch_config.get("features", {})
    return features.get(feature_name, DEFAULT_FEATURE_FLAGS.get(feature_name, False))


def is_security_review_enabled(args, dispatch_config: dict | None = None) -> bool:
    """Return True iff security review should run for this dispatch.

    Single source of truth for the security-review enablement precedence:
      1. CLI flag ``args.security_review`` wins when not None.
      2. Otherwise, fall back to ``dispatch_config["security_review"]`` (top-level).
      3. The ``features.security_review`` feature flag can disable even when
         the higher-precedence sources enable it (kill-switch semantics).

    This helper was extracted from the duplicate precedence chains that
    previously lived inline in ``equipa.cli:run_dispatch`` and
    ``equipa.dispatch:_is_security_review_enabled``. Bug 2321 was caused
    by exactly this kind of silent divergence between two code paths
    implementing the same policy; centralising the rule here prevents
    a recurrence.

    Args:
        args: argparse Namespace (or any object) with a ``security_review``
            attribute. Missing attribute is treated as None.
        dispatch_config: parsed dispatch config dict. If None, falls back
            to ``getattr(args, "dispatch_config", None)`` for callers
            (such as the dispatch.py call site) that thread the config
            through args instead of passing it explicitly.
    """
    dc = dispatch_config
    if dc is None:
        dc = getattr(args, "dispatch_config", None) or {}
    enabled = getattr(args, "security_review", None)
    if enabled is None:
        enabled = dc.get("security_review", False)
    if not is_feature_enabled(dc, "security_review"):
        enabled = False
    return bool(enabled)


def load_dispatch_config(filepath: str | Path | None) -> dict:
    """Load dispatch_config.json preferences.

    Returns a config dict with defaults for any missing keys.
    Falls back to defaults entirely if file not found.
    """
    config = dict(DEFAULT_DISPATCH_CONFIG)

    if filepath is None:
        # Default location: alongside the TheForge DB (where the orchestrator
        # script lives). Fall back to CWD-relative if that does not exist.
        filepath = Path(THEFORGE_DB).parent / "dispatch_config.json"
        if not filepath.exists():
            filepath = Path("dispatch_config.json")
    else:
        filepath = Path(filepath)

    if not filepath.exists():
        return config

    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: Could not load dispatch config '{filepath}': {e}")
        print("  Using defaults.")
        return config

    # Merge loaded values over defaults
    for key in DEFAULT_DISPATCH_CONFIG:
        if key in data:
            config[key] = data[key]

    # Deep-merge features: user's partial features dict is overlaid on defaults
    # so specifying e.g. {"features": {"hooks": true}} does not wipe other flags.
    if "features" in data and isinstance(data["features"], dict):
        merged_features = dict(DEFAULT_FEATURE_FLAGS)
        merged_features.update(data["features"])
        config["features"] = merged_features

    # Also merge any extra keys not in defaults (model_developer, model_epic, etc.)
    for key in data:
        if key not in config:
            config[key] = data[key]

    return config


# --- Orchestrator dispatch config (model resolution source) -----------------
#
# Model resolution must read the SAME dispatch config the orchestrator loaded
# for role resolution (``args.dispatch_config`` in equipa.cli), never a
# ``dispatch_config.json`` that happens to sit in the process CWD (task #2994,
# SECURITY-REVIEW-2992 S2). The orchestrator registers its loaded config via
# set_active_dispatch_config(). Processes that never register one (MCP server,
# ForgeSmith, standalone scripts) resolve, in order:
#   1. the file named by $EQUIPA_DISPATCH_CONFIG, then
#   2. <repo_root>/dispatch_config.json — next to this equipa/ package.

DISPATCH_CONFIG_ENV_VAR = "EQUIPA_DISPATCH_CONFIG"
REPO_DISPATCH_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "dispatch_config.json"
)

# Optional dispatch_config.json key: model ids an operator explicitly approves
# in addition to the configured ``model``. Any other requested model resolves
# to the configured one (allowlist, task #2994 S3).
APPROVED_MODEL_UPGRADES_KEY = "approved_model_upgrades"

_active_dispatch_config: dict | None = None


def set_active_dispatch_config(dispatch_config: dict | None) -> None:
    """Register the orchestrator's loaded dispatch config for model resolution.

    Pass None to clear the registration (tests, or a process that reloads).
    """
    global _active_dispatch_config
    if dispatch_config is not None and not isinstance(dispatch_config, dict):
        raise TypeError(
            f"dispatch_config must be a dict or None, got "
            f"{type(dispatch_config).__name__}"
        )
    _active_dispatch_config = dispatch_config


def resolve_model_config_path() -> Path:
    """Return the dispatch_config.json path used when none is registered.

    $EQUIPA_DISPATCH_CONFIG wins; otherwise the repo-root file next to the
    equipa/ package. Deliberately never CWD-relative.
    """
    override = os.environ.get(DISPATCH_CONFIG_ENV_VAR, "").strip()
    if override:
        return Path(override)
    return REPO_DISPATCH_CONFIG_PATH


def get_active_dispatch_config() -> dict:
    """Return the dispatch config that model resolution must use."""
    if _active_dispatch_config is not None:
        return _active_dispatch_config
    return load_dispatch_config(resolve_model_config_path())


def get_configured_model(dispatch_config: dict | None = None) -> str:
    """Return the configured Claude model for auxiliary (non-role) agent runs.

    Used by reflexion, RLM decomposition and any other helper that spawns
    ``claude`` outside the role-model resolution in equipa.roles. Reads
    ``dispatch_config["model"]``; with no config passed it reads the
    orchestrator's registered config (see get_active_dispatch_config), never
    a CWD-relative file. Falls back to the Opus-family DEFAULT_MODEL — EQUIPA
    never picks a cheaper model on its own (task #2992).
    """
    config = (
        dispatch_config if dispatch_config is not None
        else get_active_dispatch_config()
    )
    model = config.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return DEFAULT_MODEL


def get_approved_model_upgrades(dispatch_config: dict | None = None) -> frozenset[str]:
    """Return the operator's explicit model-upgrade allowlist (may be empty).

    Non-string and blank entries are ignored; a malformed (non-list) value is
    treated as an empty list so a typo can never widen the allowlist.
    """
    config = (
        dispatch_config if dispatch_config is not None
        else get_active_dispatch_config()
    )
    raw = config.get(APPROVED_MODEL_UPGRADES_KEY, [])
    if not isinstance(raw, list):
        print(f"WARNING: dispatch config {APPROVED_MODEL_UPGRADES_KEY!r} must "
              f"be a list of model ids; ignoring {type(raw).__name__} value")
        return frozenset()
    return frozenset(
        entry.strip() for entry in raw
        if isinstance(entry, str) and entry.strip()
    )


def is_model_allowed(model: object, dispatch_config: dict | None = None) -> bool:
    """Return True only for the configured model or an approved upgrade.

    Allowlist, not denylist (task #2994 S3): an older pinned generation such
    as ``claude-opus-4-20250514`` or a bare alias such as ``opus`` is refused
    just like ``sonnet`` — only the exact configured id is honoured.
    """
    if not isinstance(model, str) or not model.strip():
        return False
    config = (
        dispatch_config if dispatch_config is not None
        else get_active_dispatch_config()
    )
    candidate = model.strip()
    if candidate == get_configured_model(config):
        return True
    return candidate in get_approved_model_upgrades(config)


# Model families EQUIPA must never select on its own (owner directive
# 2026-09-22, task #2992). Matched as substrings so both aliases ("sonnet")
# and full ids ("claude-sonnet-4-20250514") are caught.
DOWNGRADE_MODEL_FAMILIES: tuple[str, ...] = ("sonnet", "haiku")


def is_downgrade_model(model: object) -> bool:
    """Return True if ``model`` names a sonnet- or haiku-family model."""
    if not isinstance(model, str):
        return False
    name = model.lower()
    return any(family in name for family in DOWNGRADE_MODEL_FAMILIES)


def resolve_claude_model(
    requested: str | None = None,
    dispatch_config: dict | None = None,
) -> str:
    """Return the model an auxiliary Claude call must run on.

    ``requested`` (e.g. a ``model`` key in forgesmith_config.json or a project
    role's frontmatter) is honoured only when it equals the configured
    dispatch model or appears in the operator's ``approved_model_upgrades``
    list (see is_model_allowed). Anything else — sonnet, haiku, an older
    pinned Opus, a bare alias — resolves to the configured model. A refused
    request is reported on stdout so the substitution is never silent.
    """
    config = (
        dispatch_config if dispatch_config is not None
        else get_active_dispatch_config()
    )
    configured = get_configured_model(config)
    if not isinstance(requested, str) or not requested.strip():
        return configured
    requested = requested.strip()
    if is_model_allowed(requested, config):
        return requested
    print(f"WARNING: refusing non-configured model {requested!r}; using the "
          f"configured model {configured!r} (task #2994: only the configured "
          f"model or an approved upgrade is ever run)")
    return configured


# dispatch_config.json key bounding persistent-retry mode (task #2994 S9).
# After this many consecutive capacity (429/529) failures a persistent-retry
# run stops and fails loudly — with outcome agent_overloaded when the last
# error was a 529 — instead of retrying forever on the same model.
PERSISTENT_RETRY_MAX_ATTEMPTS_KEY = "persistent_retry_max_attempts"
# Backoff caps at 5 min per wait, so 36 attempts is roughly 2.5 hours.
DEFAULT_PERSISTENT_RETRY_MAX_ATTEMPTS = 36


def get_persistent_retry_max_attempts(dispatch_config: dict | None = None) -> int:
    """Return the persistent-retry ceiling from config, validated.

    Anything that is not a positive int (including bool) falls back to the
    default rather than disabling the ceiling.
    """
    config = (
        dispatch_config if dispatch_config is not None
        else get_active_dispatch_config()
    )
    raw = config.get(PERSISTENT_RETRY_MAX_ATTEMPTS_KEY,
                     DEFAULT_PERSISTENT_RETRY_MAX_ATTEMPTS)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        print(f"WARNING: dispatch config {PERSISTENT_RETRY_MAX_ATTEMPTS_KEY!r}="
              f"{raw!r} is not a positive integer; using "
              f"{DEFAULT_PERSISTENT_RETRY_MAX_ATTEMPTS}")
        return DEFAULT_PERSISTENT_RETRY_MAX_ATTEMPTS
    return raw
