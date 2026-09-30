"""Load environment variables from .env files — zero-dependency dotenv.

Reads a .env file and injects key=value pairs into os.environ WITHOUT
overwriting values that are already set. This ensures that:
  - Shell-exported variables take precedence over .env
  - Background/nohup processes pick up keys from .env automatically

Called at the top of forge_orchestrator.py (before any equipa imports)
so that constants.py sees the populated environment.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_dotenv(env_path: Path | str | None = None) -> dict[str, str]:
    """Parse a .env file and inject variables into os.environ.

    Args:
        env_path: Explicit path to .env file. When *None*, searches upward
            from the directory containing this module (equipa/) until a .env
            file is found or the filesystem root is reached.

    Returns:
        Dict of variables that were actually injected (i.e. not already set).
    """
    if env_path is not None:
        env_file = Path(env_path)
    else:
        env_file = _find_env_file()

    if env_file is None or not env_file.is_file():
        return {}

    injected: dict[str, str] = {}
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return {}

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()

        # Skip blanks and comments
        if not line or line.startswith("#"):
            continue

        # Must contain '='
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        # Validate key: must be a non-empty shell-safe identifier
        if not key or not _is_valid_env_key(key):
            continue

        # Strip optional surrounding quotes (single or double)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]

        # Only inject if not already present in the environment
        if key not in os.environ:
            os.environ[key] = value
            injected[key] = value

    return injected


def _find_env_file() -> Path | None:
    """Walk upward from equipa/ package dir to find .env."""
    current = Path(__file__).resolve().parent  # equipa/
    # Check the package dir and one level up (project root)
    for _ in range(3):
        candidate = current / ".env"
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None



# --- Agent subprocess environment (review findings loop-03 / sandbox-03) ---
#
# The orchestrator's own environment holds credentials the agents must never
# see: the prod DSN (DATABASE_URL, PG*), GitHub tokens, API keys, and
# everything load_dotenv() injected. Agent CLIs and operator hooks therefore
# get an environment built from an ALLOWLIST instead of a copy of os.environ.

# Exact names every agent needs: a working shell, locale, and the Claude CLI's
# subscription auth token.
AGENT_ENV_ALLOWLIST: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "TERM", "TMPDIR", "TZ",
    "SHELL", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
})
# Name families passed through when set (locale categories, XDG base dirs).
AGENT_ENV_ALLOWED_PREFIXES: tuple[str, ...] = ("LC_", "XDG_")

# Setting it makes the CLI bill the API instead of the subscription, so it
# needs its own explicit opt-in on top of agent_env_passthrough.
ANTHROPIC_API_KEY_VAR = "ANTHROPIC_API_KEY"

# Credential-shaped names. A prefix-family match (LC_*, XDG_*) that also looks
# like a credential is dropped; the exact allowlist is trusted as written.
_CREDENTIAL_MARKERS: tuple[str, ...] = ("SECRET", "PASSWORD", "PASSWD")
_CREDENTIAL_SUFFIXES: tuple[str, ...] = ("_TOKEN", "_KEY")


def _looks_like_credential(name: str) -> bool:
    upper = name.upper()
    return (
        any(marker in upper for marker in _CREDENTIAL_MARKERS)
        or upper.endswith(_CREDENTIAL_SUFFIXES)
        or upper.startswith("PG")
        or upper == "DATABASE_URL"
    )


def _passthrough_names(dispatch_config: Mapping[str, Any] | None) -> set[str]:
    """Exact names ``dispatch_config['agent_env_passthrough']`` adds.

    Only valid identifiers are honoured (no wildcards). ANTHROPIC_API_KEY is
    refused unless ``agent_allow_api_key`` is literally ``True``.
    """
    if not isinstance(dispatch_config, Mapping):
        return set()
    raw = dispatch_config.get("agent_env_passthrough") or []
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        logger.warning(
            "dispatch_config.agent_env_passthrough must be a list of names; "
            "ignoring %r", raw,
        )
        return set()
    allow_api_key = dispatch_config.get("agent_allow_api_key") is True
    names: set[str] = set()
    for name in raw:
        if not isinstance(name, str) or not _is_valid_env_key(name):
            logger.warning(
                "agent_env_passthrough: ignoring invalid name %r (exact "
                "variable names only)", name,
            )
            continue
        if name == ANTHROPIC_API_KEY_VAR and not allow_api_key:
            logger.warning(
                "agent_env_passthrough: %s refused; set agent_allow_api_key "
                "true to bill the API instead of the subscription",
                ANTHROPIC_API_KEY_VAR,
            )
            continue
        names.add(name)
    return names


def build_agent_env(
    dispatch_config: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the scrubbed environment for an agent CLI or operator hook.

    Keeps only :data:`AGENT_ENV_ALLOWLIST`, set ``LC_*``/``XDG_*`` names that
    do not look like credentials, and the exact names listed in
    ``dispatch_config['agent_env_passthrough']``. Everything else is dropped,
    including DATABASE_URL, PG*, ANTHROPIC_API_KEY, GITHUB_TOKEN/GH_TOKEN and
    any *_TOKEN, *_KEY, *SECRET* or *PASSWORD* name.

    Args:
        dispatch_config: Active dispatch config (for the passthrough list and
            ``agent_allow_api_key``). None means no additions.
        environ: Source environment; defaults to ``os.environ``.
    """
    source = os.environ if environ is None else environ
    passthrough = _passthrough_names(dispatch_config)
    env: dict[str, str] = {}
    for name, value in source.items():
        if name in AGENT_ENV_ALLOWLIST or name in passthrough:
            env[name] = value
        elif (name.startswith(AGENT_ENV_ALLOWED_PREFIXES)
              and not _looks_like_credential(name)):
            env[name] = value
    return env


def _is_valid_env_key(key: str) -> bool:
    """Return True if *key* is a valid shell environment variable name."""
    if not key:
        return False
    # Must start with letter or underscore, rest alphanumeric or underscore
    if not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(c.isalnum() or c == "_" for c in key)
