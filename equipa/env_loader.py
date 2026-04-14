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

import os
from pathlib import Path


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


def _is_valid_env_key(key: str) -> bool:
    """Return True if *key* is a valid shell environment variable name."""
    if not key:
        return False
    # Must start with letter or underscore, rest alphanumeric or underscore
    if not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(c.isalnum() or c == "_" for c in key)
