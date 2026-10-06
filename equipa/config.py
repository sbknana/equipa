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
    configured_path_translations
    translate_local_path

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from equipa.constants import DEFAULT_MODEL, THEFORGE_DB

logger = logging.getLogger(__name__)

# Strings accepted as flag values besides JSON booleans (see is_feature_enabled).
_FLAG_TRUE_STRINGS = frozenset({"true", "1"})
_FLAG_FALSE_STRINGS = frozenset({"false", "0"})

# Key load_dispatch_config sets when the config file exists but cannot be read
# or parsed. Its value is the error text. Consumers must not treat such a
# config as "the operator chose the defaults".
CONFIG_LOAD_ERROR_KEY = "_config_load_error"

# Security gates that must stay ON when the config cannot be read: the file
# might have enabled them, and a gate that silently turns off on a corrupt
# config is fail-open (EQUIPA review 2026-09-29, sandbox-06).
FAIL_CLOSED_FEATURE_FLAGS: frozenset[str] = frozenset({
    "bash_security_pretooluse",
    "agent_isolation",
})


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
    # detect-and-terminate that the stream observer provides. DEFAULT FALSE.
    # The reactive stream check only covers streaming roles and runs after
    # the command has executed, so it is not a substitute. An invalid value
    # for this flag forces it ON (FAIL_CLOSED_FEATURE_FLAGS). See task 2703.
    "bash_security_pretooluse": False,
    # When True, every agent CLI runs as the separate unprivileged user in
    # dispatch_config["agent_isolation"], in its own cgroup and its own git
    # clone, with a read-only TheForge view without api_keys (equipa.isolation,
    # docs/AGENT_ISOLATION.md). DEFAULT FALSE: it needs host setup first. If
    # it is on and isolation cannot be established the dispatch is refused;
    # an invalid value or unreadable config forces it ON (fail-closed).
    "agent_isolation": False,
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
    # Default ON (EQUIPA review 2026-09-29, gate-08 / cli-02): with no config,
    # or a config missing this key, code diffs used to merge unreviewed.
    # Turning review off is now an explicit opt-out.
    "security_review": True,
    "features": dict(DEFAULT_FEATURE_FLAGS),
    "autoresearch_max_retries": 3,
}


def _parse_feature_flag(value: object) -> bool | None:
    """Return the bool a configured flag value means, or None if invalid.

    Accepts JSON booleans, the JSON integers 0 and 1, and the strings
    "true"/"false"/"1"/"0" (case and surrounding whitespace ignored).
    """
    if isinstance(value, bool):
        return value
    # bool is an int subclass; it was handled above, so this is a real int.
    if isinstance(value, int) and value in (0, 1):
        return value == 1
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _FLAG_TRUE_STRINGS:
            return True
        if normalized in _FLAG_FALSE_STRINGS:
            return False
    return None


def _invalid_flag_fallback(feature_name: str, problem: str, default: bool) -> bool:
    """Resolve a flag whose configured value cannot be used.

    Security gates in FAIL_CLOSED_FEATURE_FLAGS are treated like an
    unreadable config: ERROR and ON (BS3121-02 - main read 1/"yes"/"on" as
    ON, so strict parsing must not quietly switch the gate off). Every other
    flag logs a warning and uses its documented default.
    """
    if feature_name in FAIL_CLOSED_FEATURE_FLAGS:
        logger.error(
            "%s; security feature %r is forced ON (fail-closed) instead of "
            "silently turning off", problem, feature_name,
        )
        return True
    logger.warning("%s; using default %s", problem, default)
    return default


def _coerce_feature_flag(value: object, feature_name: str, default: bool) -> bool:
    """Strictly coerce a configured flag value to bool.

    Valid values are listed in _parse_feature_flag. Anything else - "yes",
    "off", 2, 1.0, null, a list - is a config mistake and never goes through
    Python truthiness ("false" is truthy): see _invalid_flag_fallback.
    """
    parsed = _parse_feature_flag(value)
    if parsed is not None:
        return parsed
    return _invalid_flag_fallback(
        feature_name,
        f"Feature flag {feature_name!r} has invalid value {value!r} (expected "
        'true/false, 0/1 or "true"/"false"/"1"/"0")',
        default,
    )


def is_feature_enabled(dispatch_config: dict | None, feature_name: str) -> bool:
    """Check if a feature flag is enabled.

    Reads from dispatch_config["features"][feature_name]. Falls back to
    DEFAULT_FEATURE_FLAGS if the feature is not in the config. Values are
    coerced strictly (see _coerce_feature_flag); an invalid value logs a
    warning and uses the default.

    Security flags in FAIL_CLOSED_FEATURE_FLAGS resolve to True when the
    dispatch config could not be read (load_dispatch_config marks it with
    CONFIG_LOAD_ERROR_KEY), when dispatch_config itself or "features" is not
    an object, or when their value is invalid: a config problem must never
    switch a gate off silently, so an ERROR is logged and the gate stays on.

    Returns True/False. Unknown features default to False.
    """
    default = DEFAULT_FEATURE_FLAGS.get(feature_name, False)
    if dispatch_config is None:
        return default
    if not isinstance(dispatch_config, dict):
        # IND3128-05: the same fail-closed rule as every other malformed
        # shape - a security gate stays ON, other flags use their default.
        return _invalid_flag_fallback(
            feature_name,
            f"dispatch_config is {type(dispatch_config).__name__}, not a dict "
            f"(feature {feature_name!r})",
            default,
        )

    load_error = dispatch_config.get(CONFIG_LOAD_ERROR_KEY)
    if load_error and feature_name in FAIL_CLOSED_FEATURE_FLAGS:
        logger.error(
            "dispatch config could not be loaded (%s); security feature %r "
            "is forced ON (fail-closed) instead of silently turning off",
            load_error, feature_name,
        )
        return True

    features = dispatch_config.get("features", {})
    if not isinstance(features, dict):
        return _invalid_flag_fallback(
            feature_name,
            f"dispatch_config['features'] is {type(features).__name__}, not "
            f"a dict (feature {feature_name!r})",
            default,
        )
    if feature_name not in features:
        return default
    return _coerce_feature_flag(features[feature_name], feature_name, default)


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
        enabled = dc.get("security_review", True)
    if not is_feature_enabled(dc, "security_review"):
        enabled = False
    return bool(enabled)


# Feature flag and settings section of agent isolation (equipa.isolation,
# which imports this module, so the name is repeated here).
AGENT_ISOLATION_KEY = "agent_isolation"


def default_dispatch_config_path() -> Path:
    """The dispatch_config.json a run without --dispatch-config loads."""
    # Default location: alongside the TheForge DB (where the orchestrator
    # script lives). Fall back to CWD-relative if that does not exist.
    filepath = Path(THEFORGE_DB).parent / "dispatch_config.json"
    if not filepath.exists():
        filepath = Path("dispatch_config.json")
    return filepath


def host_dispatch_config_path() -> Path:
    """The host's own dispatch config, whose safety and layout settings a
    per-run config cannot drop (see :func:`load_dispatch_config`)."""
    return default_dispatch_config_path()


def load_dispatch_config(filepath: str | Path | None) -> dict:
    """Load dispatch_config.json preferences.

    Returns a config dict with defaults for any missing keys.
    Falls back to defaults entirely if file not found.

    A per-run ``filepath`` (``--dispatch-config``) is merged over the
    defaults, not over the host's config, except for the host's safety and
    layout settings (:func:`_carry_host_settings`):

    * a fail-closed gate (``FAIL_CLOSED_FEATURE_FLAGS``) the host config
      (:func:`host_dispatch_config_path`) has on stays on, with the host's
      ``agent_isolation`` section unless the per-run file has its own (an
      empty section is not, IR87-07). A per-run config may turn such a gate
      on, never off, and a missing per-run file does not turn it off either
      (review F1);
    * the security review the host config has on stays on (IR87-05);
    * the host's ``path_translations`` stay, and a per-run file may only add
      prefixes the host does not map (IR83-01). The prefixes it adds are
      recorded (``PER_RUN_TRANSLATIONS_KEY``) and never widen the scaffold's
      mkdir allowlist (IR87-04, :func:`scaffold_root_targets`);
    * the host's MCP trust lists (``HOST_TRUST_LIST_KEYS``) stay, and a
      per-run file may only add entries (IR87-01);
    * host layout keys (``HOST_LAYOUT_KEYS``) the per-run file does not set
      keep the host's value.
    """
    if filepath is None:
        return _read_dispatch_config(default_dispatch_config_path())
    filepath = Path(filepath)
    config = _read_dispatch_config(filepath)
    # Only the carry below records which entries a per-run file added.
    config.pop(PER_RUN_TRANSLATIONS_KEY, None)
    _carry_host_settings(config, filepath)
    return config


def _same_file(first: Path, second: Path) -> bool:
    try:
        return first.resolve() == second.resolve()
    except (OSError, RuntimeError):  # RuntimeError: a link loop
        return False


# Host-config keys that describe this host's layout and that a per-run
# config which does not set them keeps from the host (task #3187): without
# the host's scaffold source every scaffold clone is refused.
HOST_LAYOUT_KEYS: tuple[str, ...] = ("forgescaffold_dir",)

# Host-config lists of what an agent's MCP servers may launch and fetch from
# (equipa.agent_runner, which imports this module, so the names are repeated
# here). A per-run config keeps the host's entries and may only add its own
# (IR87-01, task #3189): prod's uvx lives outside the system directories, so
# without the host's list every dispatch under a per-run config was refused.
HOST_TRUST_LIST_KEYS: tuple[str, ...] = (
    "mcp_trusted_executables",
    "mcp_uvx_trusted_urls",
)

# How a carried fail-closed gate is named in the warning a per-run config
# that turns it off gets.
_GATE_NAMES = {
    AGENT_ISOLATION_KEY: "agent isolation",
    "bash_security_pretooluse": "the Bash PreToolUse hook",
}


def _json_copy(value: object) -> object:
    """A deep copy of a JSON value, so a carried setting shares nothing."""
    return json.loads(json.dumps(value))


def _carry_host_settings(config: dict, per_run_path: Path) -> None:
    """Keep the host config's safety and layout settings in a per-run config.

    A per-run file is merged over the defaults, so anything the operator set
    only in the host config would otherwise silently drop out of the run.
    """
    host_path = host_dispatch_config_path()
    if _same_file(host_path, per_run_path) or not host_path.exists():
        return
    host = _read_dispatch_config(host_path)
    for gate in sorted(FAIL_CLOSED_FEATURE_FLAGS):
        _carry_host_gate(config, host, gate, per_run_path, host_path)
    _carry_host_security_review(config, host, per_run_path, host_path)
    _carry_host_isolation_section(config, host, per_run_path, host_path)
    _carry_host_path_translations(config, host, per_run_path, host_path)
    for key in HOST_TRUST_LIST_KEYS:
        _carry_host_list(config, host, key, per_run_path)
    for key in HOST_LAYOUT_KEYS:
        if key not in config and key in host:
            config[key] = _json_copy(host[key])


def _carry_host_security_review(config: dict, host: dict,
                                per_run_path: Path, host_path: Path) -> None:
    """Keep the security review on when the host config has it on (IR87-05,
    task #3189): a per-run file may turn it on, never off, by the top-level
    ``security_review`` or by ``features.security_review``. The CLI's
    ``--no-security-review`` still decides for its own run."""
    if (not is_security_review_enabled(None, host)
            or is_security_review_enabled(None, config)):
        return
    logger.warning(
        "dispatch config '%s' turns the security review off, but the host "
        "config '%s' has it on; a per-run config cannot turn the security "
        "review off", per_run_path, host_path,
    )
    config["security_review"] = True
    features = config.get("features")
    # Features that are not an object leave the flag at its default (on).
    if isinstance(features, dict):
        config["features"] = {**features, "security_review": True}


def _carry_host_isolation_section(config: dict, host: dict,
                                  per_run_path: Path, host_path: Path) -> None:
    """The host's ``agent_isolation`` settings when the host has isolation
    on and the per-run file has no settings of its own. An empty or
    non-object per-run section is not settings of its own (IR87-07, task
    #3189): it used to replace the host's section."""
    host_section = host.get(AGENT_ISOLATION_KEY)
    if (not is_feature_enabled(host, AGENT_ISOLATION_KEY)
            or not isinstance(host_section, dict) or not host_section):
        return
    own = config.get(AGENT_ISOLATION_KEY, None)
    if isinstance(own, dict) and own:
        return
    if AGENT_ISOLATION_KEY in config:
        logger.warning(
            "dispatch config '%s': %r is %r, which holds no settings; using "
            "the host config '%s' section", per_run_path, AGENT_ISOLATION_KEY,
            own, host_path,
        )
    config[AGENT_ISOLATION_KEY] = _json_copy(host_section)


def _carry_host_list(config: dict, host: dict, key: str,
                     per_run_path: Path) -> None:
    """The host's ``key`` entries, then the per-run file's own new ones.

    A per-run value that is not a list is logged and ignored; the host's
    entries stay either way. A host value that is not a list carries
    nothing (the reader logs and ignores it, as before).
    """
    host_entries = host.get(key)
    if not isinstance(host_entries, list) or not host_entries:
        return
    own = config.get(key)
    if own is None:
        own = []
    elif not isinstance(own, list):
        logger.error(
            "dispatch config '%s': %r must be a list, got %r; using only the "
            "host config's", per_run_path, key, own,
        )
        own = []
    carried = _json_copy(host_entries)
    for entry in own:
        if entry not in carried:
            carried.append(_json_copy(entry))
    config[key] = carried


def _carry_host_gate(config: dict, host: dict, gate: str,
                     per_run_path: Path, host_path: Path) -> None:
    """Keep a fail-closed gate the host config has on, on in ``config``."""
    if not is_feature_enabled(host, gate):
        return
    features = config.get("features")
    # Features that are not an object already read every fail-closed flag
    # as ON; replacing them would switch the other gates off.
    if not isinstance(features, dict):
        return
    if not is_feature_enabled(config, gate):
        logger.warning(
            "dispatch config '%s' turns %s off, but the host config '%s' "
            "has it on; a per-run config cannot turn %s off",
            per_run_path, gate, host_path, _GATE_NAMES.get(gate, gate),
        )
    # A new dict: config["features"] may be the shared default flags.
    config["features"] = {**features, gate: True}


def _translation_source_key(entry: object) -> str | None:
    """The ``from`` of a ``path_translations`` entry in comparable form:
    separators unified, trailing ones dropped, case folded (a drive letter
    is case-insensitive). None when the entry has no string ``from``."""
    if not isinstance(entry, dict) or not isinstance(entry.get("from"), str):
        return None
    source = entry["from"].strip().replace("\\", "/").rstrip("/")
    return source.casefold() or None


def _carry_host_path_translations(config: dict, host: dict,
                                  per_run_path: Path, host_path: Path) -> None:
    """Keep the host's ``path_translations`` in a per-run config (IR83-01).

    The per-run file may add prefixes the host does not map. An entry whose
    ``from`` is a host prefix, or lies under one, is dropped with a warning:
    the host decides where its own share is mounted, and the ``to`` prefixes
    are also the scaffold's mkdir allowlist (equipa.scaffold).
    """
    host_entries = host.get(PATH_TRANSLATIONS_KEY)
    if not isinstance(host_entries, list):
        host_entries = []
    own = config.get(PATH_TRANSLATIONS_KEY)
    if not host_entries and not isinstance(own, list):
        # Nothing to carry, and nothing the per-run file adds is usable.
        return
    if own is None:
        own = []
    elif not isinstance(own, list):
        logger.error(
            "dispatch config '%s': %r must be a list, got %r; using only the "
            "host config's", per_run_path, PATH_TRANSLATIONS_KEY, own,
        )
        own = []
    host_sources = {
        source for source in map(_translation_source_key, host_entries)
        if source is not None
    }
    kept = []
    for entry in own:
        source = _translation_source_key(entry)
        if source is not None and any(
                source == host_source or source.startswith(host_source + "/")
                for host_source in host_sources):
            if entry not in host_entries:
                logger.warning(
                    "dispatch config '%s': ignoring %r entry %r; the host "
                    "config '%s' maps that prefix and a per-run config cannot "
                    "re-point it", per_run_path, PATH_TRANSLATIONS_KEY, entry,
                    host_path,
                )
            continue
        kept.append(entry)
    config[PATH_TRANSLATIONS_KEY] = _json_copy(host_entries) + _json_copy(kept)
    if kept:
        config[PER_RUN_TRANSLATIONS_KEY] = _json_copy(kept)


def _read_dispatch_config(filepath: Path) -> dict:
    """``filepath`` merged over the defaults (see load_dispatch_config).

    A missing file quietly yields the defaults: this runs on every
    get_active_dispatch_config() call without a registered config (the
    repo-root file is usually absent), and stdout is the MCP server's
    JSON-RPC channel. The CLI warns about a ``--dispatch-config`` path that
    does not exist (equipa.cli.warn_missing_dispatch_config).
    """
    config = dict(DEFAULT_DISPATCH_CONFIG)

    if not filepath.exists():
        return config

    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(
                f"top-level JSON is {type(data).__name__}, expected an object"
            )
    except (OSError, ValueError) as e:
        # ValueError covers JSONDecodeError and UnicodeDecodeError. Mark the
        # config so fail-closed security flags stay on (is_feature_enabled).
        logger.error("Could not load dispatch config '%s': %s", filepath, e)
        print(f"WARNING: Could not load dispatch config '{filepath}': {e}")
        print("  Using defaults; security gates fail closed.")
        config[CONFIG_LOAD_ERROR_KEY] = f"{filepath}: {e}"
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
    Sonnet/haiku-family entries are dropped with a warning (SR-2994 S3):
    EQUIPA never runs on those families, even when an operator lists one.
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
    approved: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            continue
        model = entry.strip()
        if is_downgrade_model(model):
            print(f"WARNING: dispatch config {APPROVED_MODEL_UPGRADES_KEY!r} "
                  f"lists {model!r}, a sonnet/haiku-family model; ignoring it "
                  f"(owner directive: EQUIPA never runs on sonnet/haiku)")
            continue
        approved.add(model)
    return frozenset(approved)


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
    # A sonnet/haiku-family model is never an "upgrade", whatever the list
    # says (SR-2994 S3). get_approved_model_upgrades already drops such
    # entries; this guard keeps is_model_allowed correct on its own.
    if is_downgrade_model(candidate):
        return False
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


# --- Local path translation (task #3183, IR80-05) ---------------------------
#
# A TheForge ``projects.local_path`` can be recorded from another host: a
# Windows client that sees the project share as ``X:\share`` while the
# orchestrator sees the same share mounted at ``/srv/share``. The operator
# lists such prefixes in the dispatch config, e.g.
#
#     "path_translations": [{"from": "X:\\share", "to": "/srv/share"}]
#
# Empty by default: a path is then used as recorded. Every place that turns a
# DB ``local_path`` into a directory goes through translate_local_path().

PATH_TRANSLATIONS_KEY = "path_translations"
# Set by load_dispatch_config on a per-run config: the path_translations
# entries the per-run file added over the host config's (IR87-04).
PER_RUN_TRANSLATIONS_KEY = "_per_run_path_translations"
_PATH_SEPARATORS = "/\\"


def _has_parent_segment(path: str) -> bool:
    """True if ``path`` has a ``..`` segment under either separator."""
    return ".." in path.replace("\\", "/").split("/")


def _valid_path_translation(entry: object) -> tuple[str, str] | None:
    """``(from, to)`` of one configured entry, or None (logged) if unusable.

    ``from`` loses its trailing separators and must not become empty (that
    would match every path); ``to`` must be absolute on this host. Neither
    may hold a ``..`` segment.
    """
    if not isinstance(entry, dict):
        logger.error("dispatch config %r: ignoring entry %r (expected an "
                     'object with "from" and "to")', PATH_TRANSLATIONS_KEY, entry)
        return None
    source, target = entry.get("from"), entry.get("to")
    if not isinstance(source, str) or not isinstance(target, str):
        logger.error('dispatch config %r: ignoring entry %r ("from" and "to" '
                     "must be strings)", PATH_TRANSLATIONS_KEY, entry)
        return None
    source = source.strip().rstrip(_PATH_SEPARATORS)
    target = target.strip()
    if not source or not Path(target).is_absolute():
        logger.error('dispatch config %r: ignoring entry %r ("from" must be a '
                     'non-root prefix and "to" an absolute path)',
                     PATH_TRANSLATIONS_KEY, entry)
        return None
    if _has_parent_segment(source) or _has_parent_segment(target):
        logger.error("dispatch config %r: ignoring entry %r (a '..' segment)",
                     PATH_TRANSLATIONS_KEY, entry)
        return None
    return source, target


def configured_path_translations(
    dispatch_config: dict | None = None,
) -> list[tuple[str, str]]:
    """The usable ``(from, to)`` prefixes from ``path_translations``.

    Reads the orchestrator's registered config when none is passed (see
    get_active_dispatch_config). Invalid entries are logged and skipped, so a
    path they would have matched is used as recorded. Longest ``from`` first,
    so ``X:\\share\\sub`` wins over ``X:\\share``.
    """
    config = (
        dispatch_config if dispatch_config is not None
        else get_active_dispatch_config()
    )
    raw = config.get(PATH_TRANSLATIONS_KEY, []) if isinstance(config, dict) else []
    if raw is None:
        return []
    if not isinstance(raw, list):
        logger.error("dispatch config %r must be a list of {\"from\", \"to\"} "
                     "objects, got %r; no path is translated",
                     PATH_TRANSLATIONS_KEY, raw)
        return []
    translations = [
        pair for pair in map(_valid_path_translation, raw) if pair is not None
    ]
    return sorted(translations, key=lambda pair: len(pair[0]), reverse=True)


def scaffold_root_targets(dispatch_config: dict | None = None) -> list[str]:
    """The ``to`` prefixes under which a scaffold clone may be created.

    Every usable ``path_translations`` target except those of the entries a
    per-run config added over the host config's (IR87-04, task #3189): a
    per-run file may map a new prefix, but it cannot widen where
    :mod:`equipa.scaffold` creates directories (``{"from": "Q:", "to":
    "/"}`` would have allowed any absolute path). A record of added entries
    that is not a list trusts no target.
    """
    config = (
        dispatch_config if dispatch_config is not None
        else get_active_dispatch_config()
    )
    if not isinstance(config, dict):
        return []
    added = config.get(PER_RUN_TRANSLATIONS_KEY, [])
    if not isinstance(added, list):
        logger.error("dispatch config %r is %r, not a list; no scaffold root "
                     "is taken from %r", PER_RUN_TRANSLATIONS_KEY, added,
                     PATH_TRANSLATIONS_KEY)
        return []
    raw = config.get(PATH_TRANSLATIONS_KEY)
    if added and isinstance(raw, list):
        raw = [entry for entry in raw if entry not in added]
    host_only = {PATH_TRANSLATIONS_KEY: raw}
    return [target for _source, target in configured_path_translations(host_only)]


def translate_local_path(
    local_path: str, dispatch_config: dict | None = None,
) -> str:
    """``local_path`` with its configured ``path_translations`` prefix mapped.

    A prefix matches whole path segments only (``X:\\share`` does not match
    ``X:\\shared``), case-sensitively, with ``\\`` and ``/`` read as the same
    separator. The rest of a translated path has its ``\\`` turned into
    ``/``. A path no prefix matches is returned unchanged.

    This does not check where the result points: a caller that creates
    directories must still run its own containment check on the result.
    """
    if not isinstance(local_path, str):
        raise TypeError(
            f"local_path must be a str, got {type(local_path).__name__}"
        )
    normalized = local_path.replace("\\", "/")
    for source, target in configured_path_translations(dispatch_config):
        prefix = source.replace("\\", "/")
        if not normalized.startswith(prefix):
            continue
        rest = normalized[len(prefix):]
        if rest and not rest.startswith("/"):
            continue  # same leading letters, a different directory
        return (target.rstrip(_PATH_SEPARATORS) + rest) or "/"
    if os.name != "nt" and is_drive_letter_path(local_path):
        _warn_untranslated_drive_path(local_path)
    return local_path


def is_drive_letter_path(path: str) -> bool:
    """True if ``path`` starts with a Windows drive (``C:`` or ``C:\\``)."""
    drive_letter = path[:1]
    return (path[1:2] == ":" and drive_letter.isascii()
            and drive_letter.isalpha())


# Paths already warned about, so a project resolved on every poll logs once.
_warned_untranslated_paths: set[str] = set()


def _warn_untranslated_drive_path(local_path: str) -> None:
    """Log (once per path) a drive-letter path no translation maps (IR83-01):
    on this host it is a relative name, so the project will not resolve."""
    if local_path in _warned_untranslated_paths:
        return
    _warned_untranslated_paths.add(local_path)
    logger.warning(
        "local_path %r is a Windows drive path and no %r entry maps it; add "
        "its prefix to the host dispatch config", local_path,
        PATH_TRANSLATIONS_KEY,
    )
