"""Task #3183 (IR80-05): the operator's private paths never reappear in the
public repository, in code, comments, docs or tests.

Task #3187 (IR83-04): the scan covers every file of the tree, not a list of
directories (a marker in a new top-level directory was missed), and the
operator's host names too.

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
# The operator's host names, in prose and in identifiers (a function was
# named after the orchestrator host, a schema comment after both hosts).
OPERATOR_HOST_MARKERS: tuple[str, ...] = (
    "claud" + "inator",
    "forge" + "-inference",
    "forge" + "_inference",
)
OPERATOR_MARKERS: tuple[str, ...] = OPERATOR_PATH_MARKERS + OPERATOR_HOST_MARKERS
# Files of the tree the scan skips, each with the reason. Empty: every
# tracked file, and every untracked one git does not ignore, is public.
EXCLUDED_FILES: frozenset[str] = frozenset()
# Directories a walk of a copy without ``.git`` (``git archive``) skips:
# caches and local environments, never part of the public tree.
_SKIPPED_DIRECTORY_NAMES = frozenset({
    ".git", "__pycache__", ".pytest_cache", "node_modules", ".forge-worktrees",
    ".venv", "venv",
})


def _marker_hits(path: Path, markers: Iterable[str]) -> list[str]:
    """The markers ``path``'s bytes hold (case-insensitively)."""
    try:
        content = path.read_bytes().lower()
    except FileNotFoundError:  # listed by git, deleted in the work tree
        return []
    return [marker for marker in markers if marker.lower().encode() in content]


def _not_excluded(files: Iterable[Path], root: Path) -> list[Path]:
    return [path for path in files
            if path.relative_to(root).as_posix() not in EXCLUDED_FILES]


def _walked_files(root: Path) -> list[Path]:
    """Every file under ``root``, found by walking it (a copy without
    ``.git``, e.g. from ``git archive``), whatever its top-level folder."""
    files: list[Path] = []
    for current, subdirectories, names in os.walk(root):
        subdirectories[:] = [name for name in subdirectories
                             if name not in _SKIPPED_DIRECTORY_NAMES]
        files.extend(Path(current) / name for name in names)
    return _not_excluded(files, root)


def _git_listed(root: Path) -> list[Path]:
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
    return [root / os.fsdecode(name) for name in result.stdout.split(b"\0")
            if name]


def _git_files(root: Path) -> list[Path]:
    return _not_excluded(_git_listed(root), root)


def _public_files(root: Path) -> list[Path]:
    return _git_files(root) if (root / ".git").exists() else _walked_files(root)


def _files_with_markers(files: Iterable[Path], root: Path,
                        markers: Iterable[str] = OPERATOR_MARKERS) -> list[str]:
    markers = tuple(markers)
    return [f"{path.relative_to(root).as_posix()}: {', '.join(hits)}"
            for path in files if (hits := _marker_hits(path, markers))]


def test_no_operator_path_is_in_the_public_tree() -> None:
    files = _public_files(REPO_ROOT)
    scanned = {path.relative_to(REPO_ROOT).parts[0] for path in files}

    # Not vacuous: the scan reached every directory the task names.
    assert {"equipa", "scripts", "docs", "tests", "README.md"} <= scanned, sorted(scanned)
    assert _files_with_markers(files, REPO_ROOT, OPERATOR_PATH_MARKERS) == []


def test_no_operator_host_name_is_in_the_public_tree() -> None:
    files = _public_files(REPO_ROOT)

    assert _files_with_markers(files, REPO_ROOT, OPERATOR_HOST_MARKERS) == []


def test_the_scan_covers_every_listed_file() -> None:
    """No directory allowlist: every file git lists is scanned, whatever
    its top-level folder (a new one included). In a copy without ``.git``
    the walk is held to every top-level entry of the copy."""
    if (REPO_ROOT / ".git").exists():
        listed = set(_git_listed(REPO_ROOT))
    else:
        listed = {entry for top in REPO_ROOT.iterdir()
                  if top.name not in _SKIPPED_DIRECTORY_NAMES
                  for entry in ([top] if top.is_file() else top.rglob("*"))
                  if entry.is_file()
                  and not _SKIPPED_DIRECTORY_NAMES & set(entry.parts)}
    scanned = set(_public_files(REPO_ROOT))

    assert listed - scanned == {REPO_ROOT / name for name in EXCLUDED_FILES} & listed
    assert {path.relative_to(REPO_ROOT).parts[0] for path in scanned} == {
        path.relative_to(REPO_ROOT).parts[0] for path in listed}


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


def test_the_scan_finds_host_names_and_new_top_level_folders(
        tmp_path: Path) -> None:
    """The reviewer's two misses: a marker in a new top-level directory and
    a host name appended to a doc. Neither the hostname-free text nor the
    skipped cache directory is reported."""
    (tmp_path / "benchmarks").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "__pycache__").mkdir()
    share_form = "/srv/" + "forge" + "-share" + "/Project"
    host_form = "ssh user@" + "Claud" + "inator"
    identifier_form = "def is_on_" + "claud" + "inator():"
    inference_form = "ollama on " + "forge" + "-inference"
    (tmp_path / "benchmarks" / "run.py").write_text(
        f"ROOT = {share_form!r}\n", encoding="utf-8")
    (tmp_path / "docs" / "USER_GUIDE.md").write_text(
        f"Log in: {host_form}\n", encoding="utf-8")
    (tmp_path / "docs" / "loop.py").write_text(
        f"{identifier_form}\n    pass\n# {inference_form}\n", encoding="utf-8")
    (tmp_path / "docs" / "clean.md").write_text(
        "Run it on the orchestrator host.\n", encoding="utf-8")
    (tmp_path / "__pycache__" / "cached.pyc").write_text(host_form,
                                                         encoding="utf-8")

    found = _files_with_markers(_walked_files(tmp_path), tmp_path)

    assert sorted(found) == sorted([
        "benchmarks/run.py: " + "forge" + "-share",
        "docs/USER_GUIDE.md: " + "claud" + "inator",
        "docs/loop.py: " + "claud" + "inator, " + "forge" + "-inference",
    ])


def test_the_git_listing_reaches_a_new_top_level_folder(tmp_path: Path) -> None:
    """The git form of the scan: a tracked file in a new top-level folder
    and an untracked one are both scanned; an ignored one is not."""
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull}
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=env)
    (tmp_path / "benchmarks").mkdir()
    (tmp_path / "newtool").mkdir()
    marker_line = "host = " + "claud" + "inator\n"
    (tmp_path / "benchmarks" / "run.py").write_text(marker_line, encoding="utf-8")
    (tmp_path / "newtool" / "notes.md").write_text(marker_line, encoding="utf-8")
    (tmp_path / "local.cfg").write_text(marker_line, encoding="utf-8")
    (tmp_path / ".gitignore").write_text("local.cfg\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "benchmarks/run.py"],
                   check=True, env=env)

    found = _files_with_markers(_git_files(tmp_path), tmp_path)

    assert sorted(found) == sorted([
        "benchmarks/run.py: " + "claud" + "inator",
        "newtool/notes.md: " + "claud" + "inator",
    ])


def test_this_file_does_not_hold_the_markers() -> None:
    assert _marker_hits(Path(__file__), OPERATOR_MARKERS) == []
