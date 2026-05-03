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
ALLOWLIST_PATH = REPO_ROOT / ".deploy-allowlist"

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


# ---------------------------------------------------------------------------
# Allowlist file (.deploy-allowlist) — canonical machine-readable form
# ---------------------------------------------------------------------------


def _read_allowlist_entries() -> list[str]:
    assert ALLOWLIST_PATH.exists(), (
        f".deploy-allowlist missing at repo root: {ALLOWLIST_PATH}"
    )
    entries: list[str] = []
    for line in ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append(stripped)
    return entries


def test_deploy_allowlist_matches_fixture() -> None:
    """`.deploy-allowlist` must mirror EXPECTED_PROD_ONLY_FILES."""
    assert _read_allowlist_entries() == list(EXPECTED_PROD_ONLY_FILES)


def test_deploy_allowlist_matches_in_script_array() -> None:
    """`.deploy-allowlist` and the script's PROD_ONLY_FILES fallback must agree."""
    assert _read_allowlist_entries() == _extract_prod_only_files(_read_script())


def test_deploy_script_references_allowlist_file() -> None:
    """The script must read .deploy-allowlist, not parse markdown."""
    text = _read_script()
    assert ".deploy-allowlist" in text, (
        "deploy script must consult .deploy-allowlist as the canonical list"
    )


# ---------------------------------------------------------------------------
# Auto-bootstrap behavior — these tests build a tiny fake upstream + prod
# pair and run the deploy script end-to-end via bash.
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command with deterministic identity in tests."""
    env = {
        "GIT_AUTHOR_NAME": "deploy-test",
        "GIT_AUTHOR_EMAIL": "deploy-test@forgeborn.local",
        "GIT_COMMITTER_NAME": "deploy-test",
        "GIT_COMMITTER_EMAIL": "deploy-test@forgeborn.local",
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": str(cwd),
    }
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _build_deploy_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build (source_repo, upstream_bare, prod_repo) fixture for deploy tests.

    - source_repo: fake Equipa-repo containing the deploy script + allowlist
      and a marker `forge_orchestrator.py`.
    - upstream_bare: bare repo that source pushes to and prod pulls from.
    - prod_repo: clone of upstream playing the role of Equipa-prod.
    """
    source = tmp_path / "source"
    upstream = tmp_path / "upstream.git"
    prod = tmp_path / "prod"

    # 1. Build source repo with the marker file + a tracked source file.
    source.mkdir()
    _git(source, "init", "-q", "-b", "master")
    (source / "forge_orchestrator.py").write_text("# orchestrator marker\n")
    (source / "equipa_module.py").write_text("VERSION = 1\n")
    # equipa package the script's import check expects.
    (source / "equipa").mkdir()
    (source / "equipa" / "__init__.py").write_text("")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "initial source")

    # 2. Bare upstream that prod will pull from.
    _git(tmp_path, "init", "-q", "--bare", str(upstream))
    _git(source, "remote", "add", "origin", str(upstream))
    _git(source, "push", "-q", "origin", "master")

    # 3. Prod clone of upstream — initial state matches source exactly.
    _git(tmp_path, "clone", "-q", str(upstream), str(prod))
    _git(prod, "remote", "rename", "origin", "upstream-local")

    # 4. Move source forward by one commit so a deploy actually has work.
    (source / "equipa_module.py").write_text("VERSION = 2\n")
    (source / "new_source_file.py").write_text("# added in v2\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "v2 changes")
    _git(source, "push", "-q", "origin", "master")

    # 5. Install the real deploy script + .deploy-allowlist into source so
    #    the script can find them at runtime (it reads from $PWD).
    (source / "scripts").mkdir(exist_ok=True)
    shutil.copy2(SCRIPT_PATH, source / "scripts" / "deploy-equipa-prod.sh")
    shutil.copy2(ALLOWLIST_PATH, source / ".deploy-allowlist")

    return source, upstream, prod


def _run_deploy(source: Path, prod: Path) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None, "bash required for deploy integration tests"
    env = {
        "EQUIPA_PROD_DIR": str(prod),
        "EQUIPA_UPSTREAM_REMOTE": "upstream-local",
        "EQUIPA_UPSTREAM_BRANCH": "master",
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": str(source),
    }
    return subprocess.run(
        [bash, "scripts/deploy-equipa-prod.sh"],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_deploy_auto_bootstraps_when_prod_has_source_drift(tmp_path: Path) -> None:
    """First-deploy scenario: prod has uncommitted edits to tracked source files
    (the cp/rsync-era state). Script must auto-reset and succeed."""
    source, _upstream, prod = _build_deploy_fixture(tmp_path)

    # Simulate the bootstrap-era prod state: a tracked file modified to
    # something completely different + an untracked file that upstream's
    # newer commit DOES introduce (the original "would be overwritten" case).
    (prod / "equipa_module.py").write_text("VERSION = 'tampered-locally'\n")
    (prod / "new_source_file.py").write_text("# stale local copy\n")

    # Drop a prod-only file so the snapshot/restore path is exercised too.
    (prod / "dispatch_config.json").write_text('{"model": "opus-4.6"}\n')

    result = _run_deploy(source, prod)
    assert result.returncode == 0, (
        f"deploy failed unexpectedly:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "Auto-bootstrapping" in (result.stdout + result.stderr), (
        "expected auto-bootstrap log line. "
        f"output:\n{result.stdout}\n{result.stderr}"
    )

    # Source files now match upstream exactly.
    assert (prod / "equipa_module.py").read_text() == "VERSION = 2\n"
    assert (prod / "new_source_file.py").read_text() == "# added in v2\n"

    # Working tree is clean except for prod-only allowlisted files.
    # Step 7 of the deploy script runs `python3 -c "import equipa"`, which
    # legitimately creates __pycache__/ directories — those are not real drift.
    status = subprocess.run(
        ["git", "-C", str(prod), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().splitlines()
    untracked_or_dirty = [line[3:] for line in status if line]
    for entry in untracked_or_dirty:
        if "__pycache__" in entry:
            continue
        assert entry in EXPECTED_PROD_ONLY_FILES, (
            f"unexpected dirty entry after deploy: {entry!r}"
        )

    # Prod-only file restored, not destroyed.
    assert (prod / "dispatch_config.json").read_text() == '{"model": "opus-4.6"}\n'


def test_deploy_halts_on_unknown_files(tmp_path: Path) -> None:
    """If prod contains a file that is neither tracked upstream nor in the
    allowlist, the script must STOP rather than risk destroying real work."""
    source, _upstream, prod = _build_deploy_fixture(tmp_path)

    # A genuinely unknown file — not in upstream tree, not in allowlist.
    unknown = prod / "operator-handwritten-notes.txt"
    unknown.write_text("DO NOT DELETE - operator local hotfix\n")

    result = _run_deploy(source, prod)
    assert result.returncode != 0, (
        "deploy must abort when unknown files are present in prod"
    )
    combined = result.stdout + result.stderr
    assert "operator-handwritten-notes.txt" in combined, (
        "abort message must name the offending file. output:\n" + combined
    )
    assert "Refusing to destroy" in combined, (
        "abort must use unambiguous refusal language. output:\n" + combined
    )

    # The unknown file must still exist (not destroyed).
    assert unknown.exists(), "unknown file must NOT be deleted on halt"
    assert "DO NOT DELETE" in unknown.read_text()


def test_step8_regenerates_before_comparing(tmp_path: Path) -> None:
    """Regression guard for task #2134: Step 8 must regenerate the manifest
    BEFORE establishing its baseline hash. Comparing the just-pulled file
    against a freshly-regenerated one produced false-positive drift errors
    because forge_orchestrator.py auto-regenerates at runtime in prod."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")

    # Locate Step 8 by its log line and read everything to Step 9.
    step8_start = text.find('log "Step 8: verify skill_manifest hashes"')
    step9_start = text.find('log "Step 9:')
    assert step8_start != -1, "Step 8 log line missing"
    assert step9_start != -1 and step9_start > step8_start, "Step 9 log line missing"
    step8 = text[step8_start:step9_start]

    # The two regenerations must both happen before any failure-comparing
    # branch. We assert at least two --regenerate-manifest invocations exist
    # and that the comparison is between two post-regen hashes (not against
    # a pre-pull baseline).
    regen_count = step8.count("--regenerate-manifest")
    assert regen_count >= 2, (
        f"Step 8 must invoke --regenerate-manifest at least twice "
        f"(found {regen_count}). Single regen reintroduces the false-positive "
        f"hash drift bug from task #2134."
    )

    # The old buggy variable name MANIFEST_BEFORE captured the pre-regen file;
    # the fix renames it to a baseline-after-regen variable. This assertion
    # is intentionally narrow — it only forbids the specific variable that
    # encoded the bug, not all uses of the word "before".
    assert "MANIFEST_BEFORE=" not in step8, (
        "Step 8 still defines MANIFEST_BEFORE — the pre-regen baseline that "
        "caused false-positive drift in task #2134. Use a post-regen baseline."
    )


def test_step8_comment_documents_false_positive_fix(tmp_path: Path) -> None:
    """The Step 8 fix must be documented inline so future readers do not
    revert to the simpler-looking single-regen comparison."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    step8_start = text.find("# Step 8: verify skill_manifest hashes")
    assert step8_start != -1, "Step 8 banner comment missing"
    # Look at the comment block immediately around the Step 8 banner.
    snippet = text[max(0, step8_start - 50): step8_start + 1500]
    assert "auto-regenerate" in snippet.lower() or "auto-regen" in snippet.lower(), (
        "Step 8 must document the auto-regeneration false-positive cause "
        "so the fix is not silently reverted."
    )


def test_deploy_clean_prod_still_works(tmp_path: Path) -> None:
    """Backwards-compat: a clean prod with no drift must still deploy via
    the regular fast-forward pull path."""
    source, _upstream, prod = _build_deploy_fixture(tmp_path)
    # No drift, no prod-only files. Script should fast-forward.

    result = _run_deploy(source, prod)
    assert result.returncode == 0, (
        f"clean-prod deploy failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    # No bootstrap path on clean prod.
    assert "Auto-bootstrapping" not in (result.stdout + result.stderr)
    # Upstream changes landed.
    assert (prod / "equipa_module.py").read_text() == "VERSION = 2\n"
    assert (prod / "new_source_file.py").exists()
