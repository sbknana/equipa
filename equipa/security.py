"""EQUIPA security — untrusted content isolation and skill integrity.

Layer 5: Imports from equipa.constants only.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from equipa.constants import PROMPTS_DIR, SKILL_MANIFEST_FILE, SKILLS_BASE_DIR


def _make_untrusted_delimiter() -> str:
    """Return a unique, unpredictable delimiter for untrusted content markers."""
    return f"UNTRUSTED_{uuid.uuid4().hex[:8]}"


def wrap_untrusted(content: str, delimiter: str) -> str:
    """Wrap *content* in unpredictable untrusted-content markers.

    The delimiter is generated once per prompt build and shared across all
    injection sites so the agent sees a single, consistent boundary token.
    """
    return f"<<<{delimiter}>>>\n{content}\n<<<END_{delimiter}>>>"


# Transient debris that appears under prompts/ or skills/ when a skill script
# is executed or a test runs there. Never part of the shipped skill content, so
# excluded from the manifest — otherwise the first agent to run a skill script
# leaves a __pycache__ behind and every later dispatch is refused.
MANIFEST_EXCLUDED_DIRS = frozenset({
    "__pycache__", ".git", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "node_modules", ".venv", "venv",
})
MANIFEST_EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo", ".pyd", ".so", ".dll"})
MANIFEST_EXCLUDED_NAMES = frozenset({".DS_Store", "Thumbs.db"})


def _hash_file_bytes(data: bytes) -> str:
    """SHA-256 of *data* with text line endings normalized to LF.

    The manifest pins LF-content hashes (skill files are stored LF in git).
    Hashing raw bytes makes verification line-ending-sensitive: on a checkout
    with ``git autocrlf=true`` the working-tree files are CRLF, so raw-byte
    hashes mismatch the LF manifest on *every* file — verify_skill_integrity
    fails on Windows while passing on Linux/CI. Normalizing CRLF (and lone CR)
    to LF before hashing makes verification identical on every platform and git
    config, and leaves the existing LF manifest valid (LF content is unchanged
    by normalization, so its hashes don't move).

    Binary content (detected by a NUL byte) is hashed raw: a 0x0D byte there is
    data, not a line ending, and rewriting it would both corrupt the hash's
    meaning and let two different files collide onto one hash.
    """
    if b"\x00" in data:
        return hashlib.sha256(data).hexdigest()
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest()


# Backwards-compatible alias: the manifest covered only .md files before
# executable skill content (scripts, rule files, configs) was brought in.
_hash_md_bytes = _hash_file_bytes


def iter_manifest_files() -> list[Path]:
    """Return every prompt/skill file the manifest covers, sorted.

    Covers ALL file types, not just ``.md``. Skills ship executable content —
    ``.py`` helpers, ``.sh`` tools, ``.ql`` queries, ``.yar`` rules, JSON/YAML
    configs — that agents are instructed to run. Hashing only the prose left
    exactly the files worth tampering with unprotected.
    """
    files: list[Path] = []
    for search_dir in [PROMPTS_DIR, SKILLS_BASE_DIR]:
        if not search_dir.is_dir():
            continue
        for path in search_dir.rglob("*"):
            if not path.is_file():
                continue
            if any(part in MANIFEST_EXCLUDED_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in MANIFEST_EXCLUDED_SUFFIXES:
                continue
            if path.name in MANIFEST_EXCLUDED_NAMES:
                continue
            files.append(path)
    return sorted(files)


def generate_skill_manifest() -> dict[str, str]:
    """Scan all prompt and skill files and return a dict of {relative_path: sha256_hex}.

    Used by --regenerate-manifest to create/update skill_manifest.json.
    """
    base_dir = Path(__file__).parent.parent
    manifest: dict[str, str] = {}

    for file_path in iter_manifest_files():
        # Use POSIX separators so the manifest is portable across OSes —
        # str() would emit backslashes on Windows, breaking both the
        # startswith("prompts/") checks and verify_skill_integrity's
        # cross-platform key matching.
        rel_path = file_path.relative_to(base_dir).as_posix()
        manifest[rel_path] = _hash_file_bytes(file_path.read_bytes())

    return manifest


def write_skill_manifest() -> dict:
    """Generate and write skill_manifest.json to the repo root."""
    manifest = generate_skill_manifest()
    # No timestamp field: the manifest must be deterministic for identical
    # inputs, or every --regenerate-manifest produces git churn (open Q #460).
    manifest_data = {
        "version": 1,
        "description": "SHA-256 hashes of prompt and skill files for integrity verification",
        "files": manifest,
    }
    SKILL_MANIFEST_FILE.write_text(
        json.dumps(manifest_data, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(manifest)} file hashes to {SKILL_MANIFEST_FILE}")
    return manifest_data


def verify_skill_integrity() -> bool:
    """Verify all prompt and skill files match known-good SHA-256 hashes.

    Returns True if verification passes, False if files are tampered/missing
    or if the manifest is corrupt/empty.
    """
    if not SKILL_MANIFEST_FILE.exists():
        print("ERROR: skill_manifest.json not found")
        return False

    try:
        manifest_data = json.loads(SKILL_MANIFEST_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: Failed to load skill_manifest.json: {e}")
        return False

    expected_files = manifest_data.get("files", {})
    if not expected_files:
        print("ERROR: skill_manifest.json has empty files dict")
        return False

    base_dir = Path(__file__).parent.parent
    mismatches: list[str] = []
    missing: list[str] = []

    for rel_path, expected_hash in expected_files.items():
        file_path = base_dir / rel_path
        if not file_path.exists():
            missing.append(rel_path)
            continue
        actual_hash = _hash_file_bytes(file_path.read_bytes())
        if actual_hash != expected_hash:
            mismatches.append(rel_path)

    # Files present on disk but absent from the manifest. Checking only the
    # manifest's own entries catches edits to known files but not ADDED ones —
    # and an added file is a complete attack on its own: skills reference each
    # other by path, so a dropped-in SKILL.md or helper script is content an
    # agent will read and run, with every hash in the manifest still matching.
    untracked = [
        p.relative_to(base_dir).as_posix()
        for p in iter_manifest_files()
        if p.relative_to(base_dir).as_posix() not in expected_files
    ]

    if missing:
        print(f"ERROR: Skill integrity — {len(missing)} file(s) missing:")
        for f in missing:
            print(f"  MISSING: {f}")

    if mismatches:
        print(f"ERROR: Skill integrity — {len(mismatches)} file(s) tampered:")
        for f in mismatches:
            print(f"  TAMPERED: {f}")

    if untracked:
        print(f"ERROR: Skill integrity — {len(untracked)} file(s) not in manifest:")
        for f in untracked:
            print(f"  UNTRACKED: {f}")

    if missing or mismatches or untracked:
        return False

    return True
