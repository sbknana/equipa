"""Merge integrity: merge only reviewed commits and watch the default branch.

Task #3111 (gate-01, gate-12, dispatch-03). The security gate decides whether
a task may merge. This module makes sure the merge that follows is the one the
gate approved, and that nothing else moved the default branch meanwhile:

* :func:`snapshot_reviewed_tree` records the exact commit a security reviewer
  is about to read (``HEAD`` of the task worktree) and whether the worktree
  is clean, index flags included. The merge later uses that SHA, never the
  branch name, so a commit added to the branch after the review cannot ride
  along.
* :func:`create_review_checkout` (task #3116, MI-01) gives the reviewer an
  orchestrator-made, read-only detached checkout of that SHA, so the bytes it
  reads are the bytes that are merged.
* :class:`DefaultBranchGuard` snapshots the default branch before dispatch and
  keeps an expected-SHA chain. The only legitimate movement is the
  orchestrator's own merge — checked by parents AND by tree (MI-02); any
  other movement trips a loud ALERT, and every later merge in the run is
  refused.
* :func:`find_repo_execution_hazards` fails closed on repository state the
  hardened git helper cannot neutralise: ``refs/replace/*`` (the reviewer
  agent's own git still honours them, so it could be shown a decoy), driver
  programs in any config scope or submodule config, attributes files outside
  the reviewed tree that select a foreign driver, and a missing or changed
  global-config pin (MI-04, MI-05).

Agents share the repository's ref store, ``.git/config`` and the operator's
HOME with the orchestrator (same UID), so none of this is a sandbox. It turns
silent bypasses into loud, blocked runs.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from equipa.git_ops import (
    DefaultBranchDetectionError,
    GlobalConfigPinError,
    get_trusted_default_branch,
    git_run_async,
    git_toplevel_async,
    parse_config_list_z,
    pin_global_git_config,
    read_regular_file_bounded,
    verify_global_git_config_pin,
)
from equipa.security_gate import _gate_audit_log

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 15

# Config keys that make checkout / merge / diff / status run a program. The
# hardened git helper pins program-valued keys it can name up front, but
# these are keyed by an attribute-chosen driver name, so they cannot be
# pinned away and are refused in every scope unless allowlisted below.
_DRIVER_CONFIG_KEYS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^filter\..+\.(clean|smudge|process)$"), "filter driver"),
    (re.compile(r"^merge\..+\.driver$"), "merge driver"),
    (re.compile(r"^diff\..+\.(textconv|command)$"), "diff driver"),
)

# Config keys that move the work tree or the attribute source somewhere the
# reviewed diff does not show.
_REDIRECT_CONFIG_KEYS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^core\.worktree$"), "work-tree redirect"),
    (re.compile(r"^attr\.tree$"), "attribute-source redirect"),
    # ``--includes`` already lists what an include pulls in, under the
    # including file's scope. A conditional include is only followed when its
    # condition holds where the listing runs (onbranch:, gitdir:), which need
    # not be where the merge runs, so any includeIf fails closed.
    (re.compile(r"^includeif\..+\.path$"), "conditional include"),
)

# R3119-01 (task #3126): keys that make git report paths relative to the
# directory it runs in. From a nested project's sub-directory,
# ``diff.relative=true`` drops every change outside it from the gate diff.
# The hardened helper pins both off; a scope switching them on is refused
# (an explicit false value is harmless). ``git config --list`` lowercases keys.
_PATH_SCOPE_CONFIG_KEYS = frozenset({"diff.relative", "status.relativepaths"})
_GIT_FALSE_VALUES = frozenset({"false", "no", "off", "0", ""})

# IND-01 (task #3132): keys that hide a submodule pointer bump from
# porcelain ``git diff``, so a gitlink change plus a README read as doc-only.
# The gate diff overrides them, but any value other than "none" is refused.
_SUBMODULE_IGNORE_KEY_RE = re.compile(r"^(diff\.ignoresubmodules|submodule\..+\.ignore)$")

# Task #3116 (MI-04): driver programs allowed in any config scope, by exact
# key AND value. An agent redefining filter.lfs.smudge to its own program is
# still refused. Extend only with programs the operator installed.
DRIVER_CONFIG_ALLOWLIST: Mapping[str, frozenset[str]] = MappingProxyType({
    "filter.lfs.clean": frozenset({"git-lfs clean -- %f"}),
    "filter.lfs.smudge": frozenset({"git-lfs smudge -- %f"}),
    "filter.lfs.process": frozenset({"git-lfs filter-process"}),
})

# Driver names an attributes file OUTSIDE the reviewed tree (info/attributes,
# core.attributesFile) may select: git-lfs, git's built-in merge drivers and
# its built-in diff hunk-header patterns. Anything else there fails closed.
_BUILTIN_DIFF_DRIVERS = frozenset({
    "ada", "bash", "bibtex", "cpp", "csharp", "css", "dts", "elixir",
    "fortran", "fountain", "golang", "html", "java", "kotlin", "markdown",
    "matlab", "objc", "pascal", "perl", "php", "python", "ruby", "rust",
    "scheme", "tex",
})
_ATTRIBUTE_DRIVER_ALLOWLIST: Mapping[str, frozenset[str]] = MappingProxyType({
    "filter": frozenset({"lfs"}),
    "merge": frozenset({"text", "binary", "union"}),
    "diff": _BUILTIN_DIFF_DRIVERS,
})
_ATTRIBUTE_DRIVER_RE = re.compile(r"^(filter|merge|diff)=(.+)$")

# "command" is the hardened helper's own -c pins, never agent-written.
_TRUSTED_SCOPES = frozenset({"command"})


class MergeIntegrityError(RuntimeError):
    """The default branch could not be pinned before dispatch."""


# Gate statuses after which a task counts as done: its approved commit is on
# the default branch (merged), or was already there (noop — the branch had
# nothing to merge, the degenerate docs-only change).
MERGE_DONE_STATUSES = frozenset({"merged", "noop"})


@dataclass
class MergeAttempt:
    """What one ``dispatch._merge_task_branch`` call actually did.

    ``merged_sha`` is the task commit now on the default branch: the approved
    commit itself, or its rebased copy when the rebase fallback was used.

    ``regenerated_paths`` (task #3131) is set when the merge conflicted only
    in declared generated files and was completed by regenerating them.
    ``merged_sha`` is then still the approved commit (the merge's second
    parent) and ``post_head`` the resolution commit, which is what the gate
    records as merged: the regenerated content is part of what landed.
    """

    merged_sha: str | None = None
    pre_head: str | None = None
    post_head: str | None = None
    reason: str = ""
    regenerated_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class MergeOutcome:
    """Final gate result for one task, kept on the run's guard.

    ``status`` is the ``_gated_merge_task`` return value; ``merged_sha`` is
    set for ``merged`` / ``noop`` so a ``done`` task can be checked with
    ``git merge-base --is-ancestor <merged_sha> <default>``.
    """

    status: str
    reason: str = ""
    merged_sha: str | None = None

    @property
    def done(self) -> bool:
        return self.status in MERGE_DONE_STATUSES and bool(self.merged_sha)


def _short(sha: str | None) -> str:
    return (sha or "none")[:12]


async def resolve_commit(repo: str | os.PathLike, ref: str) -> str | None:
    """Full commit SHA ``ref`` names in ``repo``, or None if it does not exist."""
    result = await git_run_async(
        ["rev-parse", "--verify", "--quiet", "--end-of-options",
         f"{ref}^{{commit}}"],
        repo, timeout=_GIT_TIMEOUT,
    )
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


async def resolve_tree(repo: str | os.PathLike, commit: str) -> str | None:
    """Tree SHA of ``commit`` in ``repo``, or None if it does not resolve."""
    result = await git_run_async(
        ["rev-parse", "--verify", "--quiet", "--end-of-options",
         f"{commit}^{{tree}}"],
        repo, timeout=_GIT_TIMEOUT,
    )
    tree = result.stdout.strip()
    return tree if result.returncode == 0 and tree else None


async def is_ancestor(repo: str | os.PathLike, ancestor: str, descendant: str) -> bool:
    """True when ``ancestor`` is reachable from ``descendant``."""
    result = await git_run_async(
        ["merge-base", "--is-ancestor", ancestor, descendant],
        repo, timeout=_GIT_TIMEOUT,
    )
    return result.returncode == 0


async def commit_parents(repo: str | os.PathLike, sha: str) -> list[str] | None:
    """Parent SHAs of ``sha`` in order, or None when git cannot tell."""
    result = await git_run_async(
        ["rev-list", "--parents", "-n", "1", sha], repo, timeout=_GIT_TIMEOUT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[1:]


async def _range_patch_ids(
    repo: str | os.PathLike, commit_range: str,
) -> tuple[list[str], list[str]] | str:
    """(commits, patch ids) of ``commit_range`` oldest first, or a problem.

    ``--verbatim`` keeps whitespace in the patch id: in Python an indentation
    change is a code change, and the default patch id would ignore it.
    """
    commits = await git_run_async(
        ["rev-list", "--reverse", commit_range], repo, timeout=30,
    )
    if commits.returncode != 0:
        return f"rev-list {commit_range} failed: {commits.stderr.strip()[:200]}"
    log = await git_run_async(
        ["log", "--reverse", "-p", "--no-color", "--no-merges", commit_range],
        repo, timeout=60,
    )
    if log.returncode != 0:
        return f"log {commit_range} failed: {log.stderr.strip()[:200]}"
    patch_ids = await git_run_async(
        ["patch-id", "--verbatim"], repo, timeout=60,
        input=log.stdout.encode("utf-8"),
    )
    if patch_ids.returncode != 0:
        return f"patch-id {commit_range} failed: {patch_ids.stderr.strip()[:200]}"
    return (
        commits.stdout.split(),
        [line.split()[0] for line in patch_ids.stdout.splitlines() if line.strip()],
    )


async def rebased_range_problem(
    repo: str | os.PathLike, onto: str, approved_sha: str, rebased_sha: str,
) -> str | None:
    """Why ``onto..rebased_sha`` is not exactly the approved commits, or None.

    Task #3116 (MI-03): the rebase fallback re-reads HEAD of the agent's
    worktree, which another process can move right after the rebase. Before
    anything is fast-forwarded, the rebased range must sit on ``onto``, hold
    no merge commit, have exactly as many commits as ``onto..approved_sha``
    and carry the same patches in the same order.
    """
    if not rebased_sha:
        return "rebased HEAD did not resolve"
    if not await is_ancestor(repo, onto, rebased_sha):
        return f"rebased {_short(rebased_sha)} does not descend from {_short(onto)}"
    merges = await git_run_async(
        ["rev-list", "--merges", f"{onto}..{rebased_sha}"], repo, timeout=30,
    )
    if merges.returncode != 0 or merges.stdout.strip():
        return f"rebased range {_short(onto)}..{_short(rebased_sha)} contains merges"
    approved = await _range_patch_ids(repo, f"{onto}..{approved_sha}")
    if isinstance(approved, str):
        return approved
    rebased = await _range_patch_ids(repo, f"{onto}..{rebased_sha}")
    if isinstance(rebased, str):
        return rebased
    if not rebased[0] or len(rebased[0]) != len(approved[0]):
        return (
            f"rebased range has {len(rebased[0])} commit(s), the approved "
            f"range {len(approved[0])}"
        )
    if rebased[1] != approved[1]:
        return "rebased patches differ from the approved commits"
    return None


@dataclass(frozen=True)
class MergedTree:
    """``git merge-tree --write-tree`` result: the tree and its conflicted paths.

    With conflicts, ``tree`` holds the conflicted files with markers and every
    other path exactly as ``git merge`` resolves it.
    """

    tree: str
    conflicted: frozenset[str]


async def merged_tree(
    repo: str | os.PathLike, ours: str, theirs: str,
) -> MergedTree | None:
    """Merge ``ours`` and ``theirs`` from the object store alone, or None.

    Independent of the ref store and of any checkout an agent can touch
    (git >= 2.38). None when git fails or its output is inconsistent.
    """
    result = await git_run_async(
        ["merge-tree", "--write-tree", "--no-messages", "--name-only", "-z",
         ours, theirs],
        repo, timeout=60,
    )
    if result.returncode not in (0, 1):
        logger.error(
            "[Merge-Integrity] merge-tree of %s and %s failed rc=%s: %s",
            _short(ours), _short(theirs), result.returncode,
            (result.stderr or result.stdout).strip()[:200],
        )
        return None
    fields = result.stdout.split("\0")
    tree = fields[0].strip()
    conflicted = frozenset(name for name in fields[1:] if name)
    if not tree or (result.returncode == 1) != bool(conflicted):
        return None
    return MergedTree(tree, conflicted)


async def changed_paths(
    repo: str | os.PathLike, tree_a: str, tree_b: str,
) -> set[str] | None:
    """Paths whose entry differs between two trees, or None if git fails.

    Plumbing ``diff-tree`` with renames off and submodules never ignored,
    so no agent-written diff config can fold or hide a path.
    """
    result = await git_run_async(
        ["diff-tree", "-r", "-z", "--name-only", "--no-renames",
         "--ignore-submodules=none", tree_a, tree_b],
        repo, timeout=60,
    )
    if result.returncode != 0:
        return None
    return {name for name in result.stdout.split("\0") if name}


async def regenerated_resolution_problem(
    repo: str | os.PathLike,
    merged: MergedTree,
    resolved_tree: str,
    regenerated: frozenset[str],
) -> str | None:
    """Why ``resolved_tree`` is not ``merged`` with only ``regenerated`` redone.

    Task #3131: a merge that conflicted only in declared generated files is
    completed by regenerating them. The resolution may differ from the
    ``git merge-tree`` result in exactly those paths — every one of them a
    conflicted path and a regular file afterwards — and nowhere else.
    """
    if not regenerated or merged.conflicted != regenerated:
        return (
            f"conflicted paths {sorted(merged.conflicted)} are not the "
            f"regenerated paths {sorted(regenerated)}"
        )
    differing = await changed_paths(repo, merged.tree, resolved_tree)
    if differing is None:
        return f"diff-tree {_short(merged.tree)} {_short(resolved_tree)} failed"
    extra = differing - regenerated
    if extra:
        return f"the resolution also changes {sorted(extra)[:10]}"
    for path in sorted(regenerated):
        entry = await git_run_async(
            ["ls-tree", "-z", "--full-tree", resolved_tree, "--", path],
            repo, timeout=_GIT_TIMEOUT,
        )
        mode_and_type = entry.stdout.split(" ", 2)[:2]
        if entry.returncode != 0 or mode_and_type != ["100644", "blob"]:
            return f"regenerated {path} is not a regular file in the resolution"
    return None


def _driver_hazard(key: str, value: str | None) -> str | None:
    """Label of the driver program ``key`` defines, None if harmless/allowed."""
    lowered = key.lower()
    for pattern, label in _DRIVER_CONFIG_KEYS:
        if pattern.match(lowered):
            allowed = DRIVER_CONFIG_ALLOWLIST.get(lowered)
            if allowed is not None and value in allowed:
                return None
            return label
    return None


def _redirect_hazard(key: str) -> str | None:
    lowered = key.lower()
    for pattern, label in _REDIRECT_CONFIG_KEYS:
        if pattern.match(lowered):
            return label
    return None


def _path_scope_hazard(key: str, value: str | None) -> str | None:
    """Label when ``key`` switches on cwd-relative path output, else None.

    A key with no ``=`` (value None) is boolean true to git.
    """
    if key.lower() not in _PATH_SCOPE_CONFIG_KEYS:
        return None
    if value is not None and value.strip().lower() in _GIT_FALSE_VALUES:
        return None
    return "cwd-relative path output"


def _submodule_ignore_hazard(key: str, value: str | None) -> str | None:
    """Label when ``key`` hides submodule changes from ``git diff``."""
    if not _SUBMODULE_IGNORE_KEY_RE.match(key.lower()):
        return None
    if value is not None and value.strip().lower() == "none":
        return None
    return "submodule change hiding"


def _bare_repository_hazard(key: str, value: str | None) -> str | None:
    """Label when ``core.bare`` is switched on (IND-02, task #3132).

    ``git init`` writes ``core.bare = false`` into every repository, so only
    a true value counts. A bare repository has no work tree: git would
    answer for a different directory than the operator's project.
    """
    if key.lower() != "core.bare":
        return None
    if value is not None and value.strip().lower() in _GIT_FALSE_VALUES:
        return None
    return "work-tree redirect"


def _parse_scoped_config_z(raw: str) -> list[tuple[str, str, str | None]] | None:
    """(scope, key, value) triples from ``config --list --show-scope -z``.

    With ``-z`` git ends the scope with NUL and the ``key\\nvalue`` record
    with another NUL. None when the output does not have that shape.
    """
    fields = raw.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        return None
    triples: list[tuple[str, str, str | None]] = []
    for index in range(0, len(fields), 2):
        key, newline, value = fields[index + 1].partition("\n")
        triples.append((fields[index], key, value if newline else None))
    return triples


def attribute_file_hazards(path: str | os.PathLike, label: str) -> list[str]:
    """Drivers an attributes file outside the reviewed tree selects.

    ``info/attributes`` and ``core.attributesFile`` never show up in a
    reviewed diff, so any ``filter=`` / ``merge=`` / ``diff=`` there that
    names a driver outside the allowlist fails closed. An absent file is
    fine; an unreadable one (FIFO, directory, oversized) is a hazard.
    """
    try:
        data = read_regular_file_bounded(path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [f"{label} {os.fspath(path)} cannot be checked ({exc})"]
    hazards: list[str] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        tokens = line.split()
        if not tokens or tokens[0].startswith("#"):
            continue
        # tokens[0] is the path pattern, or the name of an [attr] macro.
        for token in tokens[1:]:
            match = _ATTRIBUTE_DRIVER_RE.match(token)
            if match is None:
                continue
            kind, driver = match.groups()
            if driver not in _ATTRIBUTE_DRIVER_ALLOWLIST[kind]:
                hazards.append(
                    f"{label} {os.fspath(path)} selects {kind} driver '{driver}'"
                )
    return hazards


def _default_global_attributes_file() -> Path:
    """Where git looks for global attributes when core.attributesFile is unset."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path(os.environ.get("HOME") or Path.home()) / ".config"
    return base / "git" / "attributes"


async def _git_path(repo: str | os.PathLike, *args: str) -> Path | None:
    """Absolute path printed by ``git rev-parse <args>``, None on failure."""
    result = await git_run_async(["rev-parse", *args], repo, timeout=_GIT_TIMEOUT)
    printed = result.stdout.strip()
    if result.returncode != 0 or not printed:
        return None
    path = Path(printed)
    return path if path.is_absolute() else Path(repo) / path


async def _submodule_config_hazards(repo: str | os.PathLike) -> list[str]:
    """Drivers defined in, or selected by, submodule git dirs (MI-05).

    ``git status``, and checkout / merge with ``submodule.recurse``, run git
    inside each submodule with that submodule's own config and
    ``info/attributes``. Those live under ``<common-dir>/modules/`` (nested
    submodules one ``modules/`` deeper), which the superproject's config
    listing never reads.
    """
    common = await _git_path(repo, "--git-common-dir")
    if common is None:
        return ["could not locate the common git dir to scan submodule configs"]
    modules = common / "modules"
    if not modules.is_dir():
        return []
    hazards: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(modules, followlinks=False):
        here = Path(dirpath)
        if "config" in filenames:
            config_path = here / "config"
            listing = await git_run_async(
                ["config", "--file", str(config_path), "--list", "--includes", "-z"],
                repo, timeout=_GIT_TIMEOUT,
            )
            if listing.returncode != 0:
                hazards.append(
                    f"could not read submodule config {config_path} "
                    f"(rc={listing.returncode}: {listing.stderr.strip()[:200]})"
                )
            else:
                for key, value in parse_config_list_z(listing.stdout):
                    # core.worktree is how every submodule git dir finds its
                    # checkout, so only drivers and includeIf count here.
                    label = _driver_hazard(key, value) or (
                        _redirect_hazard(key)
                        if not key.lower() == "core.worktree" else None
                    )
                    if label:
                        hazards.append(
                            f"submodule config {config_path} defines {label} '{key}'"
                        )
        if here.name == "info" and "attributes" in filenames:
            hazards.extend(attribute_file_hazards(
                here / "attributes", "submodule attributes file",
            ))
    return hazards


def _global_config_pin_hazard() -> str | None:
    """Why the orchestrator's pinned global git config is not usable."""
    try:
        pin_global_git_config()
    except GlobalConfigPinError as exc:
        return f"global git config could not be pinned ({exc})"
    return verify_global_git_config_pin()


async def find_repo_execution_hazards(repo: str | os.PathLike) -> list[str]:
    """Repository state that makes an orchestrator merge unsafe.

    Returns one human-readable reason per hazard, empty when the repo is safe
    to gate and merge. A git failure while checking is itself a hazard: this
    check fails closed.

    * ``refs/replace/*`` — the orchestrator's git ignores replace refs, but
      the security-reviewer agent's git does not, so it can review a decoy.
    * the pinned global config (task #3116, MI-04) is missing or changed
      since it was pinned. Agents share the operator's UID and HOME, so the
      real global config is agent-writable; orchestrator git reads the pin.
    * config in ANY scope git reads here (worktree, local, the pinned global;
      system config is switched off), including files pulled in via
      ``include``, that defines a filter, merge or diff driver program
      outside :data:`DRIVER_CONFIG_ALLOWLIST`, or sets ``core.worktree``,
      ``attr.tree`` or an ``includeIf``, or switches on ``diff.relative`` /
      ``status.relativePaths`` (R3119-01, task #3126), ``core.bare``, or
      sets ``diff.ignoreSubmodules`` / ``submodule.<name>.ignore`` to
      anything but ``none`` (IND-02, IND-01, task #3132).
    * ``info/attributes`` or the global attributes file selecting a driver
      outside the built-in / git-lfs set.
    * submodule git dirs defining or selecting such a driver (MI-05).
    """
    hazards: list[str] = []
    pin_problem = _global_config_pin_hazard()
    if pin_problem:
        hazards.append(pin_problem)
    replace_refs = await git_run_async(
        ["for-each-ref", "--format=%(refname)", "refs/replace/"],
        repo, timeout=_GIT_TIMEOUT,
    )
    if replace_refs.returncode != 0:
        hazards.append(
            f"could not list refs/replace/ (rc={replace_refs.returncode}: "
            f"{replace_refs.stderr.strip()[:200]})"
        )
    else:
        names = replace_refs.stdout.split()
        if names:
            shown = ", ".join(names[:3]) + (" ..." if len(names) > 3 else "")
            hazards.append(
                f"replace refs present ({len(names)}: {shown}); the reviewer "
                f"agent would see replaced objects"
            )

    config = await git_run_async(
        ["config", "--list", "--includes", "--show-scope", "-z"],
        repo, timeout=_GIT_TIMEOUT,
    )
    entries = (
        _parse_scoped_config_z(config.stdout) if config.returncode == 0 else None
    )
    if entries is None:
        hazards.append(
            f"could not read git config (rc={config.returncode}: "
            f"{config.stderr.strip()[:200]})"
        )
        return hazards
    for scope, key, value in entries:
        if scope in _TRUSTED_SCOPES:
            continue
        label = (
            _driver_hazard(key, value)
            or _redirect_hazard(key)
            or _bare_repository_hazard(key, value)
            or _path_scope_hazard(key, value)
            or _submodule_ignore_hazard(key, value)
        )
        if label:
            hazards.append(f"{scope} config defines {label} '{key}'")

    info_attributes = await _git_path(repo, "--git-path", "info/attributes")
    if info_attributes is None:
        hazards.append("could not locate info/attributes")
    else:
        hazards.extend(attribute_file_hazards(info_attributes, "info/attributes"))
    attributes_file = await git_run_async(
        ["config", "--type=path", "--get", "core.attributesFile"],
        repo, timeout=_GIT_TIMEOUT,
    )
    if attributes_file.returncode == 0 and attributes_file.stdout.strip():
        global_attributes = Path(attributes_file.stdout.strip())
        if not global_attributes.is_absolute():
            global_attributes = Path(repo) / global_attributes
    elif attributes_file.returncode == 1:  # unset: git's XDG default applies
        global_attributes = _default_global_attributes_file()
    else:
        hazards.append(
            f"could not read core.attributesFile (rc={attributes_file.returncode}: "
            f"{attributes_file.stderr.strip()[:200]})"
        )
        global_attributes = None
    if global_attributes is not None:
        hazards.extend(attribute_file_hazards(
            global_attributes, "global attributes file",
        ))
    hazards.extend(await _submodule_config_hazards(repo))
    return hazards


@dataclass(frozen=True)
class TreeSnapshot:
    """What a reviewer is (or was) looking at in a task worktree."""

    sha: str | None
    branch: str | None
    clean: bool
    detail: str = ""

    def describe(self) -> str:
        return (
            f"reviewed_sha={_short(self.sha)} branch={self.branch or 'DETACHED'} "
            f"clean={self.clean}"
            + (f" detail={self.detail}" if self.detail else "")
        )


async def index_flag_problem(worktree_dir: str | os.PathLike) -> str | None:
    """Why the index of ``worktree_dir`` cannot be trusted by ``git status``.

    Task #3116 (MI-01): ``git status`` never looks at an index entry that
    carries the skip-worktree or assume-unchanged bit, and both bits are
    agent-writable (``git update-index``). A payload committed with a benign
    copy on disk then reads as clean. ``git ls-files -v`` tags a normal
    entry ``H``; skip-worktree is ``S``, assume-unchanged lowercase, and
    any other tag (unmerged, ...) is not a clean tree either.

    ``git ls-files`` lists only the directory it runs in, so it runs at the
    work-tree root: for a nested project the reviewer's ``worktree_dir`` is
    a sub-directory (R3119-07, task #3126). The root must contain
    ``worktree_dir``: a ``core.worktree`` naming another checkout would
    otherwise check that checkout's index (IND-02, task #3132).
    """
    root = await git_toplevel_async(worktree_dir)
    if root is None:
        return f"could not locate the work-tree root of {os.fspath(worktree_dir)}"
    result = await git_run_async(["ls-files", "-v", "-z"], root, timeout=30)
    if result.returncode != 0:
        return (
            f"git ls-files failed rc={result.returncode}: "
            f"{result.stderr.strip()[:200]}"
        )
    flagged = [
        record for record in result.stdout.split("\0")
        if record and not record.startswith("H ")
    ]
    if not flagged:
        return None
    return (
        f"{len(flagged)} index entr{'y' if len(flagged) == 1 else 'ies'} not "
        f"tagged H (skip-worktree / assume-unchanged / unmerged), e.g. "
        f"{flagged[0][:160]!r}"
    )


async def _tracked_tree_problem(worktree_dir: str | os.PathLike) -> str | None:
    """Why tracked files of ``worktree_dir`` may differ from its HEAD."""
    flag_problem = await index_flag_problem(worktree_dir)
    if flag_problem:
        return flag_problem
    # MI-05: --ignore-submodules=all keeps git from running git inside a
    # submodule, with that submodule's own (agent-writable) config.
    status = await git_run_async(
        ["status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all"],
        worktree_dir, timeout=30,
    )
    if status.returncode != 0:
        return (
            f"git status failed rc={status.returncode}: "
            f"{status.stderr.strip()[:200]}"
        )
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    if dirty:
        return f"{len(dirty)} uncommitted tracked change(s), e.g. {dirty[0].strip()}"
    return None


async def snapshot_reviewed_tree(worktree_dir: str | os.PathLike) -> TreeSnapshot:
    """Record HEAD of ``worktree_dir`` and whether its tracked files are clean.

    "Clean" means no staged or unstaged change to a tracked file and no index
    entry git status would skip (MI-01). Untracked files are allowed — they
    are never merged. Repository hazards are checked first (``git status``
    could otherwise run an agent-configured filter); any hazard makes the
    snapshot unclean. The reviewer does not read this worktree: it reads an
    orchestrator-made checkout of ``sha`` (:func:`create_review_checkout`).
    """
    sha = await resolve_commit(worktree_dir, "HEAD")
    branch_res = await git_run_async(
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        worktree_dir, timeout=_GIT_TIMEOUT,
    )
    branch = branch_res.stdout.strip() if branch_res.returncode == 0 else None
    if sha is None:
        return TreeSnapshot(None, branch, False, "HEAD does not resolve")
    hazards = await find_repo_execution_hazards(worktree_dir)
    if hazards:
        return TreeSnapshot(sha, branch, False, "; ".join(hazards))
    problem = await _tracked_tree_problem(worktree_dir)
    if problem:
        return TreeSnapshot(sha, branch, False, problem)
    return TreeSnapshot(sha, branch, True)


@dataclass(frozen=True)
class ReviewCheckout:
    """An orchestrator-made, read-only checkout of the commit under review.

    Task #3116 (MI-01): the reviewer reads ``path``, a detached worktree of
    ``sha`` the orchestrator created with a fresh index, so the bytes it
    reviews are the bytes of the commit that is merged — whatever the
    developer agent did to its own worktree's index or files. Everything but
    ``writable_dir`` (where the review artifact goes) is made read-only.
    """

    repo: str
    root: Path
    path: Path
    sha: str
    writable_dir: Path


def _set_tree_read_only(top: Path, keep_writable: Path) -> None:
    """Drop write bits below ``top``; symlinks and ``keep_writable`` untouched.

    Directories are handled bottom-up so a read-only parent never blocks a
    child. ``os.chmod`` follows symlinks, so a committed link to a file
    outside the checkout is skipped rather than changed.
    """
    for dirpath, dirnames, filenames in os.walk(top, topdown=False):
        here = Path(dirpath)
        if here == keep_writable or keep_writable in here.parents:
            continue
        for name in [*filenames, *dirnames]:
            entry = here / name
            if entry == keep_writable or entry.is_symlink():
                continue
            mode = entry.lstat().st_mode
            entry.chmod(stat.S_IMODE(mode) & ~0o222)
    top.chmod(stat.S_IMODE(top.lstat().st_mode) & ~0o222)


def _restore_dir_write_bits(top: Path) -> None:
    """Give directories below ``top`` their owner write bit back for removal."""
    if not top.exists():
        return
    for dirpath, dirnames, _filenames in os.walk(top):
        here = Path(dirpath)
        here.chmod(stat.S_IMODE(here.lstat().st_mode) | 0o700)
        for name in dirnames:
            child = here / name
            if not child.is_symlink():
                child.chmod(stat.S_IMODE(child.lstat().st_mode) | 0o700)


async def review_checkout_problem(checkout: ReviewCheckout) -> str | None:
    """Why ``checkout`` no longer holds exactly ``checkout.sha``, or None."""
    head = await resolve_commit(checkout.path, "HEAD")
    if head != checkout.sha:
        return (
            f"review checkout HEAD is {_short(head)}, not the reviewed "
            f"{_short(checkout.sha)}"
        )
    return await _tracked_tree_problem(checkout.path)


async def create_review_checkout(
    repo: str | os.PathLike,
    sha: str,
    *,
    label: str,
    writable_subdir: str,
) -> ReviewCheckout:
    """Check ``sha`` out, detached, in a new orchestrator-owned temp dir.

    Raises :class:`MergeIntegrityError` when the checkout cannot be created
    or does not verify as a clean checkout of ``sha``; nothing is left
    behind in that case.
    """
    repo_path = os.fspath(repo)
    root = Path(tempfile.mkdtemp(prefix=f"equipa-review-{label}-"))
    path = root / "tree"
    added = await git_run_async(
        ["worktree", "add", "--detach", "--", str(path), sha],
        repo_path, timeout=300,
    )
    checkout = ReviewCheckout(repo_path, root, path, sha, path / writable_subdir)
    if added.returncode != 0:
        await remove_review_checkout(checkout)
        raise MergeIntegrityError(
            f"could not create a review checkout of {_short(sha)} "
            f"(rc={added.returncode}: {added.stderr.strip()[:200]})"
        )
    try:
        problem = await review_checkout_problem(checkout)
        if problem is None:
            checkout.writable_dir.mkdir(parents=True, exist_ok=True)
            _set_tree_read_only(path, checkout.writable_dir)
    except OSError as exc:
        problem = f"could not prepare the review checkout ({exc})"
    if problem:
        await remove_review_checkout(checkout)
        raise MergeIntegrityError(problem)
    return checkout


async def remove_review_checkout(checkout: ReviewCheckout) -> None:
    """Delete the review checkout and its worktree registration.

    Never raises: a leftover temp dir must not turn a finished review into a
    crash. Failures are logged.
    """
    try:
        _restore_dir_write_bits(checkout.path)
    except OSError as exc:
        logger.warning("review checkout %s: restoring write bits failed: %s",
                       checkout.path, exc)
    try:
        removed = await git_run_async(
            ["worktree", "remove", "--force", "--", str(checkout.path)],
            checkout.repo, timeout=120,
        )
        if removed.returncode != 0 and checkout.path.exists():
            logger.warning(
                "review checkout %s: git worktree remove failed rc=%s: %s",
                checkout.path, removed.returncode, removed.stderr.strip()[:200],
            )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("review checkout %s: worktree remove errored: %s",
                       checkout.path, exc)
    shutil.rmtree(checkout.root, ignore_errors=True)
    try:
        await git_run_async(["worktree", "prune"], checkout.repo, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("review checkout %s: worktree prune errored: %s",
                       checkout.path, exc)


def reviewed_commit_refusal(record, branch_sha: str | None) -> str | None:
    """Why the reviewed commit must not be merged, or None when it may be.

    ``record`` is the task's ``security_gate.ReviewerRunRecord``. The merge is
    allowed only when the reviewer started and finished on the same clean
    commit and the task branch still points at it.
    """
    if not record.reviewed_sha:
        return "reviewed SHA not recorded at reviewer start"
    if not record.reviewed_tree_clean:
        return (
            f"worktree was not clean when the review started "
            f"({record.reviewed_tree_detail or 'unknown'})"
        )
    if record.reviewed_sha_end != record.reviewed_sha:
        return (
            f"branch moved during review ({_short(record.reviewed_sha)} -> "
            f"{_short(record.reviewed_sha_end)})"
        )
    if branch_sha != record.reviewed_sha:
        return (
            f"branch moved after review (reviewed {_short(record.reviewed_sha)}, "
            f"branch now {_short(branch_sha)})"
        )
    return None


@dataclass
class DefaultBranchGuard:
    """Expected-SHA chain for the default branch over one dispatch run.

    ``expected_sha`` starts at the pre-dispatch snapshot and advances only
    through :meth:`record_merge` (the orchestrator's own merge). Any other
    movement trips the guard: :attr:`alert` is set, an ALERT naming both SHAs
    is printed and audited, and every later :meth:`verify` returns False so
    the caller merges nothing further.
    """

    project_dir: str
    default_branch: str
    baseline_sha: str
    expected_sha: str
    alert: str | None = None
    merges: list[tuple[int, str, str]] = field(default_factory=list)
    # Per-task gate result for this run, written by dispatch._gated_merge_task.
    outcomes: dict[int, MergeOutcome] = field(default_factory=dict)

    @classmethod
    async def snapshot(cls, project_dir: str | os.PathLike) -> DefaultBranchGuard:
        """Pin the trusted default branch's current SHA.

        Raises :class:`MergeIntegrityError` when the default branch cannot be
        trusted or resolved: a run that cannot be watched must not merge.

        Also pins the global git config (task #3116, MI-04) — this runs
        before any agent is dispatched, so every later orchestrator git call
        reads the config as it was before the agents could edit it.
        """
        repo = os.fspath(project_dir)
        try:
            pin_global_git_config()
        except GlobalConfigPinError as exc:
            raise MergeIntegrityError(str(exc)) from exc
        try:
            default_branch = get_trusted_default_branch(repo)
        except DefaultBranchDetectionError as exc:
            raise MergeIntegrityError(
                f"no trusted default branch in {repo}: {exc}"
            ) from exc
        sha = await resolve_commit(repo, f"refs/heads/{default_branch}")
        if sha is None:
            raise MergeIntegrityError(
                f"default branch '{default_branch}' does not resolve in {repo}"
            )
        return cls(repo, default_branch, sha, sha)

    @property
    def tripped(self) -> bool:
        return self.alert is not None

    async def current_sha(self) -> str | None:
        return await resolve_commit(
            self.project_dir, f"refs/heads/{self.default_branch}",
        )

    async def verify(self, stage: str, *, task_id: int | None = None) -> bool:
        """True while the default branch is still at the expected SHA."""
        if self.tripped:
            return False
        current = await self.current_sha()
        if current == self.expected_sha:
            return True
        self.trip(stage, current, task_id=task_id)
        return False

    async def record_merge(
        self,
        task_id: int,
        merged_sha: str,
        *,
        post_head: str | None,
        regenerated_paths: tuple[str, ...] = (),
    ) -> bool:
        """Advance the chain after the orchestrator merged ``merged_sha``.

        ``post_head`` is the SHA the orchestrator read right after its own
        merge. The new tip must be exactly that merge (task #3116, MI-02):

        * the branch still points at ``post_head``; and
        * either a fast-forward to ``merged_sha`` from a descendant of
          ``expected``, or a merge commit whose parents are
          (expected, merged_sha) AND whose tree is the tree
          ``git merge-tree --write-tree expected merged_sha`` computes — a
          forged commit with the right parents and a backdoored tree fails.

        ``regenerated_paths`` (task #3131) names the generated files the
        orchestrator regenerated to complete a conflicted merge. The only
        permitted difference from the ``merge-tree`` result is then those
        paths — which must be exactly its conflicted paths and regular files
        — see :func:`regenerated_resolution_problem`. A fast-forward never
        carries regenerated files.

        Anything else means another writer moved the branch around the
        merge, and trips the guard.
        """
        if self.tripped:
            return False
        current = await self.current_sha()
        previous = self.expected_sha
        regenerated = frozenset(regenerated_paths)
        legitimate = False
        if current is not None and current != previous and current == post_head:
            if current == merged_sha:
                legitimate = not regenerated and await is_ancestor(
                    self.project_dir, previous, current,
                )
            else:
                parents = await commit_parents(self.project_dir, current)
                legitimate = (
                    parents == [previous, merged_sha]
                    and await self._tree_is_merge_of(
                        current, previous, merged_sha, regenerated,
                    )
                )
        if not legitimate:
            self.trip(f"post-merge task={task_id}", current, task_id=task_id)
            return False
        self.expected_sha = current
        self.merges.append((task_id, previous, current))
        regenerated_note = (
            f" regenerated={','.join(sorted(regenerated))}" if regenerated else ""
        )
        _gate_audit_log(
            f"task={task_id} event=default-branch-advanced "
            f"branch={self.default_branch} before={_short(previous)} "
            f"after={_short(current)} merged_sha={_short(merged_sha)}"
            f"{regenerated_note}",
            task_id=task_id,
            event="default-branch-advanced",
        )
        return True

    async def _tree_is_merge_of(
        self,
        commit: str,
        ours: str,
        theirs: str,
        regenerated: frozenset[str] = frozenset(),
    ) -> bool:
        """True when ``commit``'s tree is the merge of ``ours`` and ``theirs``.

        ``git merge-tree --write-tree`` (git >= 2.38) recomputes the merge
        from the object store alone, independent of the ref store and of any
        checkout an agent can touch. Without ``regenerated`` the merge must be
        clean and the trees identical: a conflicted or failed recomputation is
        not a match, the orchestrator's merge would not have succeeded. With
        ``regenerated`` (task #3131) the merge must conflict in exactly those
        paths and ``commit`` may differ from the recomputed tree only there.
        """
        merged = await merged_tree(self.project_dir, ours, theirs)
        actual = await resolve_tree(self.project_dir, commit)
        if merged is None or actual is None:
            return False
        if regenerated:
            problem = await regenerated_resolution_problem(
                self.project_dir, merged, actual, regenerated,
            )
            if problem:
                logger.error(
                    "[Merge-Integrity] merge commit %s is not the merge of %s "
                    "and %s with only %s regenerated: %s",
                    _short(commit), _short(ours), _short(theirs),
                    sorted(regenerated), problem,
                )
                return False
            return True
        if merged.conflicted:
            logger.error(
                "[Merge-Integrity] merge-tree of %s and %s conflicts in %s",
                _short(ours), _short(theirs), sorted(merged.conflicted)[:10],
            )
            return False
        if actual != merged.tree:
            logger.error(
                "[Merge-Integrity] merge commit %s has tree %s, but merging %s "
                "and %s gives %s",
                _short(commit), _short(actual), _short(ours), _short(theirs),
                _short(merged.tree),
            )
            return False
        return True

    def trip(self, stage: str, actual: str | None, *, task_id: int | None = None) -> None:
        """Record an unexpected default-branch movement and raise the alarm."""
        if self.tripped:
            return
        self.alert = (
            f"default branch '{self.default_branch}' moved outside the "
            f"orchestrator's merges (stage={stage}): expected "
            f"{self.expected_sha} but found {actual or 'MISSING'}"
        )
        banner = "!" * 72
        print(f"\n{banner}")
        print(f"  [Merge-Integrity] ALERT: {self.alert}")
        print(
            f"  [Merge-Integrity] ALERT: baseline {self.baseline_sha}; "
            f"orchestrator merges this run: {len(self.merges)}. Nothing further "
            f"is merged and the affected tasks are left blocked."
        )
        print(banner)
        logger.error("[Merge-Integrity] ALERT: %s", self.alert)
        _gate_audit_log(
            f"task={task_id if task_id is not None else '-'} "
            f"event=default-branch-moved branch={self.default_branch} "
            f"stage={stage} expected={self.expected_sha} "
            f"actual={actual or 'MISSING'} baseline={self.baseline_sha}",
            task_id=task_id,
            event="default-branch-moved",
        )
