"""Task #3183 (IR80-05): the operator's private paths never reappear in the
public repository, in code, comments, docs or tests.

Task #3187 (IR83-04): the scan covers every file of the tree, not a list of
directories (a marker in a new top-level directory was missed), and the
operator's host names too.

Task #3189 (IR87-06): this file no longer holds the operator's markers, not
even in fragments (anyone could join the parts). Two kinds of check run:

* built-in generic checks, always: private IPv4 host addresses, Windows
  drive-letter share paths and home directories, each with a short list of
  placeholders the public tree uses on purpose;
* the operator's own markers (host names, share paths, addresses), read
  from an untracked local file: the path in ``EQUIPA_PRIVATE_MARKERS_FILE``,
  else ``.equipa-private-markers`` at the repository root (listed in
  ``.gitignore``). One marker per line, matched case-insensitively; blank
  lines and lines starting with ``#`` are skipped. Without the file only the
  generic checks run; a configured path that is missing or holds no marker
  fails. A marker split into string literals joined with ``+`` (or written
  side by side) is still found. See docs/DEPLOYMENT.md.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

PRIVATE_MARKERS_ENV = "EQUIPA_PRIVATE_MARKERS_FILE"
DEFAULT_PRIVATE_MARKERS_FILE = REPO_ROOT / ".equipa-private-markers"
# A shorter marker matches ordinary words and code everywhere.
MIN_MARKER_LENGTH = 4

# Files of the tree the scan skips, each with the reason. Empty: every
# tracked file, and every untracked one git does not ignore, is public.
EXCLUDED_FILES: frozenset[str] = frozenset()
# Directories a walk of a copy without ``.git`` (``git archive``) skips:
# caches and local environments, never part of the public tree.
_SKIPPED_DIRECTORY_NAMES = frozenset({
    ".git", "__pycache__", ".pytest_cache", "node_modules", ".forge-worktrees",
    ".venv", "venv",
})

# --- generic check: private IPv4 host addresses ---------------------------------

# RFC 1918 and the shared (CGNAT) range a tailnet hands out.
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
))
# Addresses the public tree uses on purpose, each the same on every host:
# test fixtures (none is any operator's address) and two fixed resolvers.
SYNTHETIC_PRIVATE_ADDRESSES = frozenset({
    "10.1.2.3",           # tests: a packet the agent firewall rejects
    "10.9.8.7",           # tests: a scripted local address
    "192.168.1.21",       # tests: a scripted local address
    "192.168.7.20",       # tests: a scripted local address
    "192.168.7.21",       # tests: a scripted local address
    "100.101.102.103",    # tests: a scripted tailnet address
    "100.100.100.100",    # the fixed tailnet DNS resolver
    "10.255.255.254",     # the fixed WSL DNS resolver
})
_IPV4_RE = re.compile(
    rb"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?(?![\d.]*\d)")

# --- generic check: Windows drive-letter share paths ----------------------------

# A drive letter, separators, then a folder name of two or more characters.
# Not after a word character (``https://``), ``?``/``(`` (a regex's
# ``(?i:\b``) or ``{``/``$`` (a shell expansion such as ``${s:\\x}``).
_DRIVE_PATH_RE = re.compile(
    rb"(?<![\w?({$])([A-Za-z]):[\\/]+([A-Za-z0-9_$][A-Za-z0-9_.$-]+)")
# Folder names the public tree uses as placeholders after a drive letter.
PLACEHOLDER_DRIVE_FOLDERS = frozenset({
    "share", "shared", "other", "proj", "project", "projects", "path",
    "users", "program", "equipa", "temp", "tmp", "windows", "example",
})

# --- generic check: home directories ----------------------------------------------

_HOME_RE = re.compile(
    rb"(?:/home/|/Users/|[A-Za-z]:[\\/]+Users[\\/]+)([A-Za-z0-9_$-][A-Za-z0-9_.$-]*)")
# User names the public tree uses as placeholders. A name starting with a
# dot (``/home/.bashrc``, ``/Users/...``) is not a user's directory.
PLACEHOLDER_USERS = frozenset({
    "user", "username", "someone", "agent", "op", "operator", "orch", "u",
    "o", "you", "me", "example", "equipa", "equipa-agent",
})

# String literals joined with ``+`` or written side by side: a marker split
# into parts is searched for with the parts joined.
_LITERAL_JOIN_RE = re.compile(rb"[\"']\s*\+?\s*[\"']")


class PrivateMarkersError(AssertionError):
    """The operator's marker file is configured but cannot be used."""


def private_markers_path(environ: dict[str, str] | None = None) -> Path | None:
    """The operator's marker file: the path ``EQUIPA_PRIVATE_MARKERS_FILE``
    names, else the default file when it exists, else None."""
    environ = os.environ if environ is None else environ
    configured = environ.get(PRIVATE_MARKERS_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_PRIVATE_MARKERS_FILE if DEFAULT_PRIVATE_MARKERS_FILE.is_file() else None


def load_private_markers(path: Path) -> tuple[str, ...]:
    """The markers in ``path``, one per line (``#`` lines and blank lines
    skipped). Fails when the file cannot be read or holds no usable marker:
    a configured file that checks nothing must not read as a clean tree."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise PrivateMarkersError(
            f"{PRIVATE_MARKERS_ENV} / {path}: cannot read the operator's "
            f"marker file: {error}") from error
    markers = tuple(line.strip() for line in lines
                    if line.strip() and not line.strip().startswith("#"))
    if not markers:
        raise PrivateMarkersError(f"{path}: the operator's marker file holds "
                                  f"no marker")
    short = [marker for marker in markers if len(marker) < MIN_MARKER_LENGTH]
    if short:
        raise PrivateMarkersError(
            f"{path}: {len(short)} marker(s) shorter than {MIN_MARKER_LENGTH} "
            f"characters would match ordinary text")
    return markers


def operator_markers() -> tuple[str, ...]:
    path = private_markers_path()
    return () if path is None else load_private_markers(path)


# --- the checks -------------------------------------------------------------------


def _searchable(content: bytes, *, lower: bool = True) -> bytes:
    """``content`` (in lower case unless ``lower`` is False), followed by
    the same text with its string literals joined."""
    if lower:
        content = content.lower()
    return content + b"\n" + _LITERAL_JOIN_RE.sub(b"", content)


def _generic_view(content: bytes) -> bytes:
    """What the generic checks read: ``content`` as written (``/Users/``
    and drive letters keep their case), then with its literals joined."""
    return _searchable(content, lower=False)


def _unique(hits: Iterable[str]) -> list[str]:
    """``hits`` once each, in order (the joined copy repeats most)."""
    return list(dict.fromkeys(hits))


def _marker_hits(path: Path, markers: Iterable[str]) -> list[str]:
    """The markers ``path``'s bytes hold (case-insensitively, also when
    split into joined string literals)."""
    try:
        content = _searchable(path.read_bytes())
    except FileNotFoundError:  # listed by git, deleted in the work tree
        return []
    return [marker for marker in markers if marker.lower().encode() in content]


def private_address_hits(content: bytes) -> list[str]:
    """Private IPv4 host addresses in ``content``. A network written as a
    range (``10.0.0.0/8``) is not a host; an address with a prefix that is
    not the network itself (a host address with its prefix, as ``ip addr``
    prints it) is."""
    hits = []
    for match in _IPV4_RE.finditer(content):
        text = match.group(1).decode("ascii")
        try:
            address = ipaddress.IPv4Address(text)
        except ValueError:
            continue
        if not any(address in network for network in PRIVATE_NETWORKS):
            continue
        prefix = match.group(2)
        if prefix is not None:
            try:
                ipaddress.ip_network(f"{text}/{prefix.decode('ascii')}")
                continue
            except ValueError:
                pass
        if text not in SYNTHETIC_PRIVATE_ADDRESSES:
            hits.append(text)
    return _unique(hits)


def drive_path_hits(content: bytes) -> list[str]:
    """Windows drive-letter paths whose first folder is not a placeholder."""
    return _unique([f"{match.group(1).decode()}:\\{match.group(2).decode()}"
            for match in _DRIVE_PATH_RE.finditer(content)
            if match.group(2).decode().casefold() not in PLACEHOLDER_DRIVE_FOLDERS])


def home_directory_hits(content: bytes) -> list[str]:
    """Home directories (``/home/<name>``, ``/Users/<name>``,
    ``C:\\Users\\<name>``) of a user that is not a placeholder."""
    return _unique([match.group(0).decode("utf-8", "replace")
            for match in _HOME_RE.finditer(content)
            if match.group(1).decode().casefold() not in PLACEHOLDER_USERS])


GENERIC_CHECKS: dict[str, Callable[[bytes], list[str]]] = {
    "private address": private_address_hits,
    "drive-letter share path": drive_path_hits,
    "home directory": home_directory_hits,
}
PATH_CHECKS = ("drive-letter share path", "home directory")
HOST_CHECKS = ("private address",)


def _generic_hits(path: Path, checks: Iterable[str]) -> list[str]:
    try:
        content = _generic_view(path.read_bytes())
    except FileNotFoundError:
        return []
    return [f"{name} {hit}" for name in checks for hit in GENERIC_CHECKS[name](content)]


# --- which files are public ---------------------------------------------------------


def _not_excluded(files: Iterable[Path], root: Path) -> list[Path]:
    markers_file = private_markers_path()
    own = {markers_file.resolve()} if markers_file is not None else set()
    return [path for path in files
            if path.relative_to(root).as_posix() not in EXCLUDED_FILES
            and path.resolve() not in own]


def _walked_files(root: Path) -> list[Path]:
    """Every file under ``root``, found by walking it (a copy without
    ``.git``, e.g. from ``git archive``), whatever its top-level folder."""
    files: list[Path] = []
    for current, subdirectories, names in os.walk(root):
        subdirectories[:] = [name for name in subdirectories
                             if name not in _SKIPPED_DIRECTORY_NAMES]
        files.extend(Path(current) / name for name in names)
    return _not_excluded(files, root)


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}


def _git_listed(root: Path) -> list[Path]:
    """Every tracked file, and every untracked one git does not ignore (a
    new file is caught before it is committed; a host's own ignored files,
    such as a production config or the marker file, are not the public
    repository's)."""
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "ls-files", "-z", "--cached",
         "--others", "--exclude-standard"],
        cwd=root, capture_output=True, check=False, env=_git_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return [root / os.fsdecode(name) for name in result.stdout.split(b"\0")
            if name]


def _git_files(root: Path) -> list[Path]:
    return _not_excluded(_git_listed(root), root)


def _public_files(root: Path) -> list[Path]:
    return _git_files(root) if (root / ".git").exists() else _walked_files(root)


def _files_with_markers(files: Iterable[Path], root: Path,
                        markers: Iterable[str]) -> list[str]:
    markers = tuple(markers)
    return [f"{path.relative_to(root).as_posix()}: {', '.join(hits)}"
            for path in files if (hits := _marker_hits(path, markers))]


def _files_with_generic_hits(files: Iterable[Path], root: Path,
                             checks: Iterable[str] = tuple(GENERIC_CHECKS)) -> list[str]:
    checks = tuple(checks)
    return [f"{path.relative_to(root).as_posix()}: {', '.join(hits)}"
            for path in files if (hits := _generic_hits(path, checks))]


# --- the public tree --------------------------------------------------------------


def test_no_operator_path_is_in_the_public_tree() -> None:
    """Generic: no drive-letter share path and no home directory that is
    not a placeholder."""
    files = _public_files(REPO_ROOT)
    scanned = {path.relative_to(REPO_ROOT).parts[0] for path in files}

    # Not vacuous: the scan reached every directory the task names.
    assert {"equipa", "scripts", "docs", "tests", "README.md"} <= scanned, sorted(scanned)
    assert _files_with_generic_hits(files, REPO_ROOT, PATH_CHECKS) == []


def test_no_operator_host_name_is_in_the_public_tree() -> None:
    """Generic: no private IPv4 host address that is not a fixture. Host
    names are the operator's own markers (next test)."""
    files = _public_files(REPO_ROOT)

    assert _files_with_generic_hits(files, REPO_ROOT, HOST_CHECKS) == []


def test_no_private_marker_is_in_the_public_tree() -> None:
    """The operator's markers, when the local file is present (no skip
    without it: the generic checks above still ran)."""
    markers = operator_markers()
    files = _public_files(REPO_ROOT)

    assert _files_with_markers(files, REPO_ROOT, markers) == []


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


def test_this_file_does_not_hold_the_markers() -> None:
    assert _marker_hits(Path(__file__), operator_markers()) == []
    assert _generic_hits(Path(__file__), GENERIC_CHECKS) == []


def test_the_default_marker_file_is_never_committed() -> None:
    """The default file name is ignored, so the operator's copy stays out of
    ``git add -A`` and out of the public listing."""
    name = DEFAULT_PRIVATE_MARKERS_FILE.name
    if (REPO_ROOT / ".git").exists():
        result = subprocess.run(["git", "check-ignore", "-q", "--no-index", name],
                                cwd=REPO_ROOT, check=False, env=_git_env())
        assert result.returncode == 0, f"{name} is not ignored by .gitignore"
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert name in ignored


# --- the operator's marker file ------------------------------------------------------

# Synthetic markers: no operator's, so the controls below can name them.
SYNTHETIC_HOST = "examplehost-3189"
SYNTHETIC_SHARE = "example_share_3189"


def _markers_file(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "markers.txt"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def test_the_marker_file_comes_from_the_environment_first(tmp_path: Path) -> None:
    configured = _markers_file(tmp_path, SYNTHETIC_HOST)

    assert private_markers_path({PRIVATE_MARKERS_ENV: str(configured)}) == configured
    assert private_markers_path({PRIVATE_MARKERS_ENV: "  "}) == (
        DEFAULT_PRIVATE_MARKERS_FILE if DEFAULT_PRIVATE_MARKERS_FILE.is_file() else None)


def test_the_marker_file_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    path = _markers_file(tmp_path, "# the orchestrator host", "",
                         f"  {SYNTHETIC_HOST}  ", SYNTHETIC_SHARE)

    assert load_private_markers(path) == (SYNTHETIC_HOST, SYNTHETIC_SHARE)


@pytest.mark.parametrize("lines", [(), ("# only a comment", "   "), ("abc",)],
                         ids=["empty", "comments-only", "too-short"])
def test_a_marker_file_that_checks_nothing_fails(
    tmp_path: Path, lines: tuple[str, ...],
) -> None:
    with pytest.raises(PrivateMarkersError):
        load_private_markers(_markers_file(tmp_path, *lines))


def test_a_configured_marker_file_that_is_missing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: the operator asked for the check, so a mistyped path
    must not read as a clean tree."""
    monkeypatch.setenv(PRIVATE_MARKERS_ENV, str(tmp_path / "missing.txt"))

    with pytest.raises(PrivateMarkersError, match="cannot read"):
        operator_markers()


def test_without_a_marker_file_only_the_generic_checks_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PRIVATE_MARKERS_ENV, raising=False)
    monkeypatch.setattr(f"{__name__}.DEFAULT_PRIVATE_MARKERS_FILE",
                        tmp_path / "absent")

    assert operator_markers() == ()


def test_the_scan_finds_each_marker_in_each_form(tmp_path: Path) -> None:
    """Positive control: a marker from the file is found whatever its case
    and wherever it sits (a docstring, a Windows path, a generated id), and
    when split into joined string literals."""
    markers = load_private_markers(_markers_file(tmp_path, SYNTHETIC_SHARE))
    tree = tmp_path / "tree"
    (tree / "docs").mkdir(parents=True)
    (tree / "equipa").mkdir()
    (tree / "tests").mkdir()
    upper = SYNTHETIC_SHARE.upper()
    middle = len(SYNTHETIC_SHARE) // 2
    (tree / "README.md").write_text(f"see /srv/{SYNTHETIC_SHARE}/P\n", encoding="utf-8")
    (tree / "equipa" / "module.py").write_text(
        f'"""Mapped from Z:\\{upper}\\Project."""\n', encoding="utf-8")
    (tree / "docs" / "diagram.mmd").write_text(
        f"  srv_{SYNTHETIC_SHARE}_repo\n", encoding="utf-8")
    (tree / "tests" / "split.py").write_text(
        f'MARKER = "{SYNTHETIC_SHARE[:middle]}" + "{SYNTHETIC_SHARE[middle:]}"\n'
        f"OTHER = '{SYNTHETIC_SHARE[:middle]}' '{SYNTHETIC_SHARE[middle:]}'\n",
        encoding="utf-8")
    (tree / "docs" / "clean.md").write_text("X:\\share and /srv/share\n",
                                            encoding="utf-8")

    found = _files_with_markers(_walked_files(tree), tree, markers)

    assert sorted(found) == sorted([
        f"README.md: {SYNTHETIC_SHARE}",
        f"equipa/module.py: {SYNTHETIC_SHARE}",
        f"docs/diagram.mmd: {SYNTHETIC_SHARE}",
        f"tests/split.py: {SYNTHETIC_SHARE}",
    ])


def test_the_scan_finds_host_names_and_new_top_level_folders(
        tmp_path: Path) -> None:
    """The reviewer's two misses: a marker in a new top-level directory and
    a host name appended to a doc. Neither the clean text nor the skipped
    cache directory is reported."""
    markers = load_private_markers(_markers_file(tmp_path, SYNTHETIC_HOST,
                                                 SYNTHETIC_SHARE))
    tree = tmp_path / "tree"
    for directory in ("benchmarks", "docs", "__pycache__"):
        (tree / directory).mkdir(parents=True)
    host_form = f"ssh user@{SYNTHETIC_HOST.title()}"
    (tree / "benchmarks" / "run.py").write_text(
        f"ROOT = '/srv/{SYNTHETIC_SHARE}/Project'\n", encoding="utf-8")
    (tree / "docs" / "USER_GUIDE.md").write_text(f"Log in: {host_form}\n",
                                                 encoding="utf-8")
    (tree / "docs" / "loop.py").write_text(
        f"def is_on_{SYNTHETIC_HOST.replace('-', '_')}():\n    pass\n"
        f"# ollama on {SYNTHETIC_HOST}\n", encoding="utf-8")
    (tree / "docs" / "clean.md").write_text("Run it on the orchestrator host.\n",
                                            encoding="utf-8")
    (tree / "__pycache__" / "cached.pyc").write_text(host_form, encoding="utf-8")

    found = _files_with_markers(_walked_files(tree), tree, markers)

    assert sorted(found) == sorted([
        f"benchmarks/run.py: {SYNTHETIC_SHARE}",
        f"docs/USER_GUIDE.md: {SYNTHETIC_HOST}",
        f"docs/loop.py: {SYNTHETIC_HOST}",
    ])


def test_the_git_listing_reaches_a_new_top_level_folder(tmp_path: Path) -> None:
    """The git form of the scan: a tracked file in a new top-level folder
    and an untracked one are both scanned; an ignored one is not."""
    markers = load_private_markers(_markers_file(tmp_path, SYNTHETIC_HOST))
    tree = tmp_path / "tree"
    env = {**_git_env(), "GIT_CONFIG_GLOBAL": os.devnull}
    subprocess.run(["git", "init", "-q", str(tree)], check=True, env=env)
    (tree / "benchmarks").mkdir()
    (tree / "newtool").mkdir()
    marker_line = f"host = {SYNTHETIC_HOST}\n"
    (tree / "benchmarks" / "run.py").write_text(marker_line, encoding="utf-8")
    (tree / "newtool" / "notes.md").write_text(marker_line, encoding="utf-8")
    (tree / "local.cfg").write_text(marker_line, encoding="utf-8")
    (tree / ".gitignore").write_text("local.cfg\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tree), "add", "benchmarks/run.py"],
                   check=True, env=env)

    found = _files_with_markers(_git_files(tree), tree, markers)

    assert sorted(found) == sorted([
        f"benchmarks/run.py: {SYNTHETIC_HOST}",
        f"newtool/notes.md: {SYNTHETIC_HOST}",
    ])


def test_the_marker_file_inside_the_tree_is_not_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    markers_file = _markers_file(tree, SYNTHETIC_HOST)
    (tree / "notes.md").write_text("clean\n", encoding="utf-8")
    monkeypatch.setenv(PRIVATE_MARKERS_ENV, str(markers_file))

    assert _files_with_markers(_walked_files(tree), tree, operator_markers()) == []


# --- the generic checks ----------------------------------------------------------------


def _address(*octets: int) -> str:
    return ".".join(str(octet) for octet in octets)


def _windows(*parts: str) -> str:
    return "\\".join(parts)


def _posix(*parts: str) -> str:
    return "/".join(parts)


def test_a_private_host_address_is_found() -> None:
    """Every private range, an address with a host prefix as ``ip addr``
    prints it, and one split into joined literals."""
    lan, tailnet, office = _address(192, 168, 50, 9), _address(100, 77, 1, 2), \
        _address(172, 20, 3, 4)
    split = f'"{_address(10, 20)}" + ".30.40"'
    content = (f"host {lan}\nnode {tailnet}:8080\nvpn {office}\n"
               f"inet {_address(10, 44, 0, 7)}/24 brd\n{split}\n").encode()

    hits = private_address_hits(_generic_view(content))

    assert set(hits) == {lan, tailnet, office, _address(10, 44, 0, 7),
                         _address(10, 20, 30, 40)}


def test_ranges_fixtures_and_public_addresses_are_not_hosts() -> None:
    content = (f"{_address(10, 0, 0, 0)}/8 {_address(172, 16, 0, 0)}/12 "
               f"{_address(192, 168, 0, 0)}/16 {_address(100, 64, 0, 0)}/10\n"
               f"{_address(10, 9, 8, 7)} {_address(127, 0, 0, 1)} "
               f"{_address(8, 8, 8, 8)} {_address(203, 0, 113, 5)}\n"
               f"version {_address(1, 10, 0, 1, 2)} {_address(999, 1, 1, 1)}\n").encode()

    assert private_address_hits(_generic_view(content)) == []


def test_a_drive_letter_share_path_is_found() -> None:
    real = _windows("Z:", "Team_Files", "Project")
    escaped = _windows("Y:", "", "Archive", "", "x")
    forward = _posix("Q:", "Media", "x")
    content = f"local_path = {real}\npath = '{escaped}'\n{forward}\n".encode()

    assert drive_path_hits(_generic_view(content)) == [
        _windows("Z:", "Team_Files"), _windows("Y:", "Archive"),
        _windows("Q:", "Media")]


def test_placeholder_drive_paths_urls_and_regex_flags_are_not_found() -> None:
    content = (f"{_windows('X:', 'share', 'Proj')} {_windows('C:', 'Users')} "
               f"{_windows('C:', 'Program Files')} {_windows('C:', 'x')}\n"
               "https://example.com/a file:///tmp/x (?i:\\bsqlcmd\\b)\n"
               "'y:\\n    pass'\n").encode()

    assert drive_path_hits(_generic_view(content)) == []


def test_a_home_directory_is_found() -> None:
    linux = _posix("", "home", "jdoe", ".config")
    mac = _posix("", "Users", "alice", "src")
    windows = _windows("C:", "Users", "bob", "AppData")
    content = f"{linux}\n{mac}\n{windows}\n".encode()

    assert home_directory_hits(_generic_view(content)) == [
        _posix("", "home", "jdoe"), _posix("", "Users", "alice"),
        _windows("C:", "Users", "bob")]


def test_placeholder_home_directories_are_not_found() -> None:
    content = (f"{_posix('', 'home', 'user', 'x')} {_posix('', 'home', '.bashrc')} "
               f"{_posix('', 'Users', '...')} {_posix('', 'home', 'agent')}\n"
               f"{_windows('C:', 'Users', '...')}\n").encode()

    assert home_directory_hits(_generic_view(content)) == []


def test_the_generic_checks_report_planted_files(tmp_path: Path) -> None:
    """End to end over a walked tree: each check reports its file."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "hosts.md").write_text(
        f"inference at {_address(192, 168, 50, 9)}\n", encoding="utf-8")
    (tmp_path / "docs" / "paths.md").write_text(
        f"{_windows('Z:', 'Team_Files', 'P')} {_posix('', 'home', 'jdoe')}\n",
        encoding="utf-8")
    (tmp_path / "docs" / "clean.md").write_text("X:\\share /home/user\n",
                                                encoding="utf-8")

    found = _files_with_generic_hits(_walked_files(tmp_path), tmp_path)

    assert sorted(found) == sorted([
        f"docs/hosts.md: private address {_address(192, 168, 50, 9)}",
        f"docs/paths.md: drive-letter share path {_windows('Z:', 'Team_Files')}, "
        f"home directory {_posix('', 'home', 'jdoe')}",
    ])
