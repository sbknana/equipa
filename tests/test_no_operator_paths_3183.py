"""Task #3183 (IR80-05): the operator's private paths never reappear in the
public repository, in code, comments, docs or tests.

The markers are built from parts, so this file does not hold them.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Searched case-insensitively; each catches the Windows (drive letter) and
# the Linux (mount) form of the operator's share, and identifiers derived
# from them (a generated diagram once had them as node ids).
OPERATOR_PATH_MARKERS: tuple[str, ...] = (
    "forge" + "-share",
    "forge" + "_share",
    "ai" + "_stuff",
)
# The public tree: the directories the task names, the other source
# directories, and the files at the repository root.
SCANNED_DIRECTORIES: tuple[str, ...] = (
    "equipa", "scripts", "docs", "tests", "prompts", "skills", "hooks", "tools",
    "examples", "standing_orders", ".github", ".githooks", ".claude",
)
_SKIPPED_DIRECTORY_NAMES = frozenset({"__pycache__", ".pytest_cache", "node_modules"})


def _marker_hits(path: Path, markers: Iterable[str]) -> list[str]:
    """The markers ``path``'s bytes hold (case-insensitively)."""
    try:
        content = path.read_bytes().lower()
    except FileNotFoundError:  # listed by git, deleted in the work tree
        return []
    return [marker for marker in markers if marker.lower().encode() in content]


def _walked_files(root: Path) -> list[Path]:
    """Every file of the public tree under ``root``, found by walking it
    (a copy without ``.git``, e.g. from ``git archive``)."""
    files = [entry for entry in root.iterdir() if entry.is_file()]
    for directory in SCANNED_DIRECTORIES:
        top = root / directory
        if not top.is_dir():
            continue
        for current, subdirectories, names in os.walk(top):
            subdirectories[:] = [name for name in subdirectories
                                 if name not in _SKIPPED_DIRECTORY_NAMES]
            files.extend(Path(current) / name for name in names)
    return files


def _git_files(root: Path) -> list[Path]:
    """Every tracked file, and every untracked one git does not ignore (a
    new file is caught before it is committed; a host's own ignored files,
    such as a production config, are not the public repository's)."""
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "ls-files", "-z", "--cached",
         "--others", "--exclude-standard"],
        cwd=root, capture_output=True, check=False,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    listed = [Path(os.fsdecode(name)) for name in result.stdout.split(b"\0") if name]
    return [root / name for name in listed
            if len(name.parts) == 1 or name.parts[0] in SCANNED_DIRECTORIES]


def _public_files(root: Path) -> list[Path]:
    return _git_files(root) if (root / ".git").exists() else _walked_files(root)


def _files_with_markers(files: Iterable[Path], root: Path) -> list[str]:
    return [f"{path.relative_to(root)}: {', '.join(hits)}"
            for path in files if (hits := _marker_hits(path, OPERATOR_PATH_MARKERS))]


def test_no_operator_path_is_in_the_public_tree() -> None:
    files = _public_files(REPO_ROOT)
    scanned = {path.relative_to(REPO_ROOT).parts[0] for path in files}

    # Not vacuous: the scan reached every directory the task names.
    assert {"equipa", "scripts", "docs", "tests", "README.md"} <= scanned, sorted(scanned)
    assert _files_with_markers(files, REPO_ROOT) == []


def test_the_scan_finds_each_marker_in_each_form(tmp_path: Path) -> None:
    """Positive control: a planted marker is found whatever its case and
    wherever it sits (a docstring, a Windows path, a generated id)."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "equipa").mkdir()
    windows_form = "Z:" + "\\" + "AI" + "_Stuff" + "\\Project"
    linux_form = "/srv/" + "forge" + "-share" + "/Project"
    diagram_form = "srv_" + "forge" + "_share_" + "AI" + "_Stuff_repo"
    (tmp_path / "README.md").write_text(f"see {linux_form}\n", encoding="utf-8")
    (tmp_path / "equipa" / "module.py").write_text(
        f'"""Mapped from {windows_form}."""\n', encoding="utf-8")
    (tmp_path / "docs" / "diagram.mmd").write_text(f"  {diagram_form}\n", encoding="utf-8")
    (tmp_path / "docs" / "clean.md").write_text("X:\\share and /srv/share\n",
                                                encoding="utf-8")

    found = _files_with_markers(_walked_files(tmp_path), tmp_path)

    assert sorted(found) == sorted([
        "README.md: " + "forge" + "-share",
        "equipa/module.py: " + "ai" + "_stuff",
        "docs/diagram.mmd: " + "forge" + "_share, " + "ai" + "_stuff",
    ])


def test_this_file_does_not_hold_the_markers() -> None:
    assert _marker_hits(Path(__file__), OPERATOR_PATH_MARKERS) == []
