"""Tests for scripts/deploy-equipa-prod.sh and docs/PROD_ONLY.md.example.

These tests guard the deploy discipline established by task #2118:

1. The deploy script must always parse cleanly under ``bash -n`` so a
   broken shell quote can never reach a production host.
2. The list of production-only files baked into the script (the
   ``PROD_ONLY_FILES`` array) must match the documented list in
   ``docs/PROD_ONLY.md.example`` and a static fixture below. If they
   diverge, either the docs lie or the script is missing a file — both
   silently break production.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "deploy-equipa-prod.sh"
DOCS_TEMPLATE = REPO_ROOT / "docs" / "PROD_ONLY.md.example"

# Source of truth for what files are production-only. Update this fixture
# (and PROD_ONLY.md.example, and the script's PROD_ONLY_FILES array) when
# adding a new prod-only file.
EXPECTED_PROD_ONLY_FILES: tuple[str, ...] = (
    "dispatch_config.json",
    "forge_config.json",
    "mcp_config.json",
    "theforge.db",
    ".env",
)


def _read_script() -> str:
    assert SCRIPT_PATH.exists(), f"deploy script missing: {SCRIPT_PATH}"
    return SCRIPT_PATH.read_text(encoding="utf-8")


def _extract_prod_only_files(script_text: str) -> list[str]:
    """Pull entries out of the ``PROD_ONLY_FILES=( ... )`` bash array.

    We deliberately parse rather than ``source`` the script so the test
    has no side effects on the host.
    """
    match = re.search(
        r"PROD_ONLY_FILES=\(\s*(?P<body>.*?)\s*\)",
        script_text,
        re.DOTALL,
    )
    assert match is not None, "PROD_ONLY_FILES array not found in deploy script"
    body = match.group("body")
    # Strip line comments before extracting quoted strings.
    body_no_comments = re.sub(r"#[^\n]*", "", body)
    return re.findall(r'"([^"]+)"', body_no_comments)


def test_deploy_script_exists_and_is_executable() -> None:
    assert SCRIPT_PATH.exists(), "scripts/deploy-equipa-prod.sh must exist"
    mode = SCRIPT_PATH.stat().st_mode
    assert mode & 0o111, "deploy script must be executable (chmod +x)"


def test_deploy_script_passes_bash_syntax_check() -> None:
    """``bash -n`` must accept the script. This catches quoting bugs early."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available on this host")
    result = subprocess.run(
        [bash, "-n", str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"bash -n failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_deploy_script_uses_strict_mode() -> None:
    """``set -euo pipefail`` is mandatory; without it errors are silent."""
    text = _read_script()
    assert "set -euo pipefail" in text, "deploy script must use strict mode"


def test_deploy_script_has_no_destructive_defaults() -> None:
    """Guard against ``rm -rf`` ever creeping in as a default cleanup step."""
    text = _read_script()
    # Allow comments to mention rm -rf, but no actual command should use it.
    code_lines = [
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "rm -rf" not in code, (
        "deploy script must not use destructive 'rm -rf' as a default action"
    )


def test_prod_only_files_match_fixture() -> None:
    text = _read_script()
    actual = _extract_prod_only_files(text)
    assert actual == list(EXPECTED_PROD_ONLY_FILES), (
        f"PROD_ONLY_FILES in deploy script ({actual}) does not match the "
        f"static fixture ({list(EXPECTED_PROD_ONLY_FILES)}). If you added a "
        "new prod-only file, update both the script and this test fixture, "
        "and document it in docs/PROD_ONLY.md.example."
    )


def test_prod_only_template_documents_every_file() -> None:
    assert DOCS_TEMPLATE.exists(), (
        f"docs/PROD_ONLY.md.example missing: {DOCS_TEMPLATE}"
    )
    content = DOCS_TEMPLATE.read_text(encoding="utf-8")
    for rel_path in EXPECTED_PROD_ONLY_FILES:
        # Each file must have its own section header in the docs template.
        # Backticks in markdown can use either single or triple form, so we
        # just look for the path appearing somewhere in a heading line.
        heading_pattern = re.compile(
            rf"^#{{1,6}}\s+`?{re.escape(rel_path)}`?\s*$",
            re.MULTILINE,
        )
        assert heading_pattern.search(content), (
            f"docs/PROD_ONLY.md.example is missing a section for {rel_path!r}"
        )


def test_deploy_script_records_rollback_commit() -> None:
    """The script must capture the prod HEAD before pulling so a rollback
    is possible. Regression guard for the ad-hoc cp/rsync incident."""
    text = _read_script()
    assert "PROD_COMMIT_BEFORE" in text
    assert "rev-parse HEAD" in text


def test_deploy_script_uses_ff_only_pull() -> None:
    """Non-fast-forward pulls indicate prod has diverged — fail loudly
    rather than silently merging."""
    text = _read_script()
    assert "git -C" in text and "pull --ff-only" in text, (
        "deploy script must use 'git pull --ff-only' to refuse divergent prod state"
    )


def test_deploy_script_snapshots_before_pull() -> None:
    """Snapshot step must precede the git pull step in the script body."""
    text = _read_script()
    snapshot_idx = text.find("snapshot production-only files")
    pull_idx = text.find("pull --ff-only")
    assert snapshot_idx != -1, "snapshot step must exist"
    assert pull_idx != -1, "pull step must exist"
    assert snapshot_idx < pull_idx, (
        "prod-only files must be snapshotted BEFORE git pull"
    )
