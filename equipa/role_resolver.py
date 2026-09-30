"""EQUIPA project-scoped role resolution.

Resolves a role name to its prompt file + config. Base roles come from the
shared ``prompts/`` set; a project may ADD new roles in a private
``<project_root>/.equipa/roles/`` overlay.

Isolation guarantee
-------------------
Two projects that each define a role of the same name (e.g. both have a
``cybersecurity-engineer``) are fully isolated: resolution is keyed on the
dispatching project's directory and holds NO process-global mutable role
state. This is what makes it safe under ``--auto-run``, which dispatches
multiple projects concurrently in one process — project A's role can never
shadow or leak into project B's. The only process-level state is the
orchestrator-written overlay-source registry below, which is itself keyed
per project root.

Overlay trust boundary (SR-2994 S1)
-----------------------------------
A project overlay lives in the TARGET repo, which agents can write. Before
this fix an overlay could replace ANY role — including the ``tester`` and
``security-reviewer`` prompts that gate the merge of the very diff that
planted the overlay — and raise its own ``turns`` / ``early_term_exempt``.
Overlays are now constrained four ways:

1. **Reserved names.** An overlay may never shadow a base role
   (:data:`RESERVED_ROLE_NAMES`, anything in ``ROLE_PROMPTS``, or any
   ``prompts/<role>.md``). Overlays may only ADD new role names.
2. **Stable source.** Overlays are read from the stable project root at a
   pinned commit (the pre-dispatch default-branch SHA registered by the
   orchestrator via :func:`pin_overlay_ref`, else the default branch's
   current commit) — never from an agent worktree or a working tree an agent
   can edit. "Default branch" is the operator-named branch from
   ``git_ops.get_trusted_default_branch``, never the agent-writable
   ``origin/HEAD`` (SR-2997 S1). Only a project with no ``.git`` entry at or
   above its root and no registered pin (so it can have no worktrees) falls
   back to reading its overlay directory from disk; any git error otherwise
   disables overlays (SR-2997 S2).
3. **Operator caps.** Overlay ``turns`` and ``effort`` are capped at operator
   dispatch-config values; ``early_term_exempt`` is honoured only for role
   names on the operator's ``early_term_exempt_project_roles`` allowlist.
   (Overlay ``model`` was already allowlisted by task #2994 S7.)
4. **Gate.** ``security_gate.decide_merge_gate`` fails closed on any diff
   touching ``.equipa/roles/`` so a new overlay always needs operator review.

Self-describing roles
---------------------
A role ``.md`` may declare its own config via optional frontmatter at the
top of the file (``model``, ``turns``, ``effort``, ``early_term_exempt``,
``skills``). Absent frontmatter, behaviour is identical to the legacy
global role set — no base ``constants.py`` edits are required to add a
project role.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import equipa.constants as _equipa_constants
from equipa.constants import (
    DEFAULT_ROLE_TURNS,
    EARLY_TERM_EXEMPT_ROLES,
    PROMPTS_DIR,
)

logger = logging.getLogger(__name__)

# Relative location of a project's private role overlay, joined onto the
# stable project root.
PROJECT_ROLES_SUBDIR = (".equipa", "roles")

# Directory (under the project root) the orchestrator creates agent worktrees
# in — see dispatch.run_parallel_tasks. A path below it is never a trusted
# overlay source, even if the orchestrator did not register it.
WORKTREE_BASE_DIRNAME = ".forge-worktrees"

# Base role names an overlay may NEVER shadow (SR-2994 S1). The merge-gating
# roles are listed explicitly so they stay reserved even if their prompt file
# is missing or ROLE_PROMPTS discovery failed; every other base role is
# reserved dynamically by is_reserved_role().
RESERVED_ROLE_NAMES: frozenset[str] = frozenset({
    "developer",
    "tester",
    "security-reviewer",
    "code-reviewer",
})

# A project role name must be a plain file stem. Anything else (path
# separators, "..", leading dot) is never looked up in an overlay.
_OVERLAY_ROLE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

# Operator dispatch-config keys bounding what an overlay may request.
PROJECT_ROLE_MAX_TURNS_KEY = "project_role_max_turns"
PROJECT_ROLE_MAX_EFFORT_KEY = "project_role_max_effort"
EARLY_TERM_EXEMPT_PROJECT_ROLES_KEY = "early_term_exempt_project_roles"

# Claude CLI effort levels, lowest to highest.
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
# Effort cap when the operator configures neither a project-role cap nor a
# global "effort".
DEFAULT_PROJECT_ROLE_MAX_EFFORT = "high"

# Hardening for every git call on a trust path: never run fsmonitor or hook
# programs from repo config, never honour refs/replace/ substitutions.
_GIT_SAFE_CONFIG = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=")
_GIT_TIMEOUT_SECONDS = 15

# Orchestrator-written overlay-source registry. Keys are resolved absolute
# paths; values are only ever written by the orchestrator (dispatch), never
# derived from agent-writable files.
_registry_lock = threading.Lock()
_worktree_roots: dict[str, str] = {}
_pinned_refs: dict[str, str] = {}
# Branch each pin was taken from, so the next pin can be checked against it.
_pinned_branches: dict[str, str] = {}
# Roots whose overlays the orchestrator refused to pin (fail closed): root -> reason.
_blocked_roots: dict[str, str] = {}


@dataclass
class RoleConfig:
    """Resolved role: its prompt body (frontmatter stripped) + optional config."""

    name: str
    path: Path
    body: str
    model: str | None = None
    turns: int | None = None
    effort: str | None = None
    early_term_exempt: bool | None = None
    skills: list[str] = field(default_factory=list)
    is_project_role: bool = False


def _coerce(raw: str):
    """Coerce a frontmatter scalar/inline-list string to a Python value."""
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [p.strip().strip("'\"") for p in inner.split(",") if p.strip()]
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw.strip("'\"")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split an optional leading ``--- ... ---`` frontmatter block from a role file.

    Returns ``(meta, body)``. When no well-formed frontmatter block is present,
    ``meta`` is empty and ``body`` is the original text unchanged.

    Intentionally minimal (no PyYAML dependency): supports scalar
    ``key: value`` lines and inline lists ``key: [a, b, c]``. A block without a
    closing ``---`` fence is treated as ordinary body text, not frontmatter.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    for i in range(1, len(lines)):
        stripped = lines[i].strip()
        if stripped == "---":
            body = "".join(lines[i + 1:])
            return meta, body.lstrip("\n")
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, raw = stripped.partition(":")
        meta[key.strip()] = _coerce(raw.strip())
    # No closing fence — not frontmatter; treat the whole text as body.
    return {}, text


# --- Overlay-source registry (written by the orchestrator only) -------------

def _registry_key(path: str | os.PathLike) -> str:
    return str(Path(path).resolve())


def register_worktree_root(
    worktree_dir: str | os.PathLike, project_root: str | os.PathLike,
) -> None:
    """Record that ``worktree_dir`` is an agent worktree of ``project_root``.

    Role resolution for ``worktree_dir`` then reads overlays from
    ``project_root`` (at its pinned ref), never from the worktree.
    """
    with _registry_lock:
        _worktree_roots[_registry_key(worktree_dir)] = _registry_key(project_root)


def pin_overlay_ref(
    project_root: str | os.PathLike, commit_sha: str, *, branch: str | None = None,
) -> None:
    """Pin the commit overlays for ``project_root`` are read from.

    The orchestrator calls this with the pre-dispatch default-branch SHA, so
    nothing an agent commits during the dispatch — even onto the default
    branch — can change which role overlays apply. ``branch`` records which
    trusted branch the SHA came from; pinning lifts any :func:`block_overlays`.
    """
    if not isinstance(commit_sha, str) or not re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", commit_sha.strip(),
    ):
        raise ValueError(f"pin_overlay_ref needs a full commit SHA, got {commit_sha!r}")
    key = _registry_key(project_root)
    with _registry_lock:
        _pinned_refs[key] = commit_sha.strip()
        if branch is not None:
            _pinned_branches[key] = branch
        _blocked_roots.pop(key, None)


def current_overlay_pin(project_root: str | os.PathLike) -> tuple[str | None, str] | None:
    """``(branch, sha)`` of the pin registered for ``project_root``, or None."""
    key = _registry_key(project_root)
    with _registry_lock:
        sha = _pinned_refs.get(key)
        return None if sha is None else (_pinned_branches.get(key), sha)


def block_overlays(project_root: str | os.PathLike, reason: str) -> None:
    """Disable every project overlay of ``project_root`` (fail closed).

    Called by the orchestrator when it refuses to pin (no trusted default
    branch, or the default branch moved in a way a pin may not follow). The
    previous pin is kept so the next dispatch is still checked against it;
    only a successful :func:`pin_overlay_ref` lifts the block.
    """
    with _registry_lock:
        _blocked_roots[_registry_key(project_root)] = reason


def clear_overlay_registry() -> None:
    """Forget every registered worktree, pin and block (tests / reload)."""
    with _registry_lock:
        _worktree_roots.clear()
        _pinned_refs.clear()
        _pinned_branches.clear()
        _blocked_roots.clear()


def stable_project_root(project_dir: str | os.PathLike) -> Path:
    """Map ``project_dir`` to the project root overlays are trusted from.

    An orchestrator-registered worktree maps to its project root. A path
    inside ``<root>/.forge-worktrees/`` maps to ``<root>`` even when
    unregistered (derived from the path string, not from the agent-writable
    ``.git`` file). Any other directory is its own root.
    """
    resolved = Path(project_dir).resolve()
    with _registry_lock:
        registered = _worktree_roots.get(str(resolved))
    if registered is not None:
        return Path(registered)
    parts = resolved.parts
    if WORKTREE_BASE_DIRNAME in parts:
        return Path(*parts[:parts.index(WORKTREE_BASE_DIRNAME)])
    return resolved


# --- Reading overlays from the trusted source -------------------------------

def is_reserved_role(role: str) -> bool:
    """True if ``role`` is a base role name that no overlay may shadow."""
    if role in RESERVED_ROLE_NAMES or role in _equipa_constants.ROLE_PROMPTS:
        return True
    return (
        bool(_OVERLAY_ROLE_NAME_RE.fullmatch(role))
        and (PROMPTS_DIR / f"{role}.md").is_file()
    )


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    """Run a hardened read-only git command in ``root``; None if git failed to run.

    Output is bytes (``cat-file blob`` must be read verbatim).
    """
    # Late import: git_ops imports this module. git_run applies the same
    # argv and env hardening as every other orchestrator git call (task
    # #3112), including the pinned global config (task #3116, MI-04).
    from equipa.git_ops import git_run

    try:
        return git_run(
            list(args), root, timeout=_GIT_TIMEOUT_SECONDS,
            env={"GIT_TERMINAL_PROMPT": "0"}, text=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("role overlay: git %s failed in %s: %s", args[0], root, exc)
        return None


@dataclass(frozen=True)
class _OverlaySource:
    """Where a project's overlays are read from.

    ``ref`` is set for git projects (read ``<ref>:<prefix>.equipa/roles/``);
    None means a non-git project whose overlay directory is read from disk.
    """

    root: Path
    prefix: str = ""
    ref: str | None = None


def _default_branch_commit(root: Path) -> str | None:
    """Current commit of ``root``'s trusted default branch, or None.

    Uses the operator-named branch (SR-2997 S1), never ``origin/HEAD``.
    """
    from equipa.git_ops import UntrustedDefaultBranchError, get_trusted_default_branch

    try:
        branch = get_trusted_default_branch(root)
    except UntrustedDefaultBranchError as exc:
        logger.warning("role overlay: %s; project overlays disabled", exc)
        return None
    proc = _run_git(root, "rev-parse", "--verify", "--quiet",
                    f"refs/heads/{branch}^{{commit}}")
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.decode("ascii", errors="replace").strip() or None


def _has_git_entry(root: Path) -> bool:
    """True if ``root`` or any parent has a ``.git`` entry (dir, file or link)."""
    return any(os.path.lexists(directory / ".git") for directory in (root, *root.parents))


def _overlay_source(project_dir: str | os.PathLike) -> _OverlaySource | None:
    """Trusted overlay source for ``project_dir``; None when there is none.

    A git project whose pinned/default commit cannot be resolved has NO
    overlay source (fail closed) rather than falling back to the filesystem.
    SR-2997 S2: a failing git command is NOT proof of "not a git repo" — git
    also fails on a corrupt ``.git/HEAD``, a broken worktree ``.git`` file or
    ``safe.directory`` refusal. The disk fallback is therefore taken only when
    no ``.git`` entry exists at or above the root and no pin is registered.
    """
    root = stable_project_root(project_dir)
    with _registry_lock:
        blocked_reason = _blocked_roots.get(str(root))
        pinned = str(root) in _pinned_refs
    if blocked_reason is not None:
        logger.warning("role overlay: overlays for %s are blocked (%s)",
                       root, blocked_reason)
        return None
    prefix_proc = _run_git(root, "rev-parse", "--show-prefix")
    if prefix_proc is None:
        return None
    if prefix_proc.returncode != 0:
        if pinned or _has_git_entry(root):
            stderr = prefix_proc.stderr.decode("utf-8", errors="replace").strip()
            logger.warning("role overlay: git failed in %s (%s); project "
                           "overlays disabled", root, stderr[:300])
            return None
        # No git repository at all: no worktrees can exist, so the project's
        # own overlay directory is the only source.
        return _OverlaySource(root=root)
    prefix = prefix_proc.stdout.decode("utf-8", errors="replace").strip()
    with _registry_lock:
        ref = _pinned_refs.get(str(root))
    if ref is None:
        ref = _default_branch_commit(root)
    if ref is None:
        logger.warning("role overlay: no default-branch commit for %s; "
                       "project overlays disabled", root)
        return None
    return _OverlaySource(root=root, prefix=prefix, ref=ref)


def _overlay_repo_path(source: _OverlaySource, role: str) -> str:
    return f"{source.prefix}{'/'.join(PROJECT_ROLES_SUBDIR)}/{role}.md"


def _git_tree_entry(source: _OverlaySource, repo_path: str) -> str | None:
    """Object id of ``repo_path`` at ``source.ref`` if it is a regular file."""
    proc = _run_git(source.root, "ls-tree", "-z", source.ref, "--", repo_path)
    if proc is None or proc.returncode != 0 or not proc.stdout:
        return None
    entry = proc.stdout.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    meta, _, _name = entry.partition("\t")
    fields = meta.split()
    # Only plain blobs: a symlink (120000) or gitlink is never a role file.
    if len(fields) != 3 or fields[1] != "blob" or fields[0] not in ("100644", "100755"):
        return None
    return fields[2]


def _read_project_overlay(
    role: str, project_dir: str | os.PathLike | None,
) -> tuple[Path, str] | None:
    """Return ``(display_path, text)`` of a project role overlay, or None.

    Never consulted for reserved (base) role names.
    """
    if not project_dir or is_reserved_role(role):
        return None
    if not _OVERLAY_ROLE_NAME_RE.fullmatch(role):
        return None
    source = _overlay_source(project_dir)
    if source is None:
        return None
    display_path = source.root.joinpath(*PROJECT_ROLES_SUBDIR, f"{role}.md")
    if source.ref is None:
        if display_path.is_file() and not display_path.is_symlink():
            return display_path, display_path.read_text(encoding="utf-8")
        return None
    object_id = _git_tree_entry(source, _overlay_repo_path(source, role))
    if object_id is None:
        if display_path.is_file():
            logger.warning(
                "role overlay %s exists on disk but is not committed on the "
                "default branch at %s; ignored", display_path, source.ref[:12])
        return None
    blob = _run_git(source.root, "cat-file", "blob", object_id)
    if blob is None or blob.returncode != 0:
        return None
    try:
        return display_path, blob.stdout.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("role overlay %s is not valid UTF-8; ignored", display_path)
        return None


def _overlay_role_names(project_dir: str | os.PathLike) -> set[str]:
    """Non-reserved role names defined by the project's trusted overlay."""
    source = _overlay_source(project_dir)
    if source is None:
        return set()
    if source.ref is None:
        overlay_dir = source.root.joinpath(*PROJECT_ROLES_SUBDIR)
        stems = (
            {f.stem for f in overlay_dir.glob("*.md") if f.is_file()}
            if overlay_dir.is_dir() else set()
        )
    else:
        overlay_prefix = f"{source.prefix}{'/'.join(PROJECT_ROLES_SUBDIR)}/"
        proc = _run_git(source.root, "ls-tree", "-z", "--name-only",
                        source.ref, "--", overlay_prefix)
        if proc is None or proc.returncode != 0:
            return set()
        stems = {
            name.rsplit("/", 1)[-1][:-len(".md")]
            for name in proc.stdout.decode("utf-8", errors="replace").split("\0")
            if name.endswith(".md")
        }
    return {
        stem for stem in stems
        if not stem.startswith("_")
        and _OVERLAY_ROLE_NAME_RE.fullmatch(stem)
        and not is_reserved_role(stem)
    }


# --- Operator caps on overlay config ----------------------------------------

def _operator_config() -> dict:
    """The operator's dispatch config (see equipa.config.get_active_dispatch_config)."""
    from equipa.config import get_active_dispatch_config

    try:
        config = get_active_dispatch_config()
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("role overlay: could not load dispatch config (%s); "
                       "applying default overlay caps", exc)
        return {}
    return config if isinstance(config, dict) else {}


def _positive_int(value: object) -> int | None:
    # bool is an int subclass; "turns: true" is not a turn count.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _cap_overlay_turns(role: str, requested: object, config: dict) -> int | None:
    turns = _positive_int(requested)
    if turns is None:
        return None
    cap = _positive_int(config.get(PROJECT_ROLE_MAX_TURNS_KEY))
    if cap is None:
        cap = max(DEFAULT_ROLE_TURNS.values(), default=_equipa_constants.DEFAULT_MAX_TURNS)
    if turns > cap:
        logger.warning("project role %s requested turns=%d; capped at %d",
                       role, turns, cap)
        return cap
    return turns


def _cap_overlay_effort(role: str, requested: object, config: dict) -> str | None:
    if not isinstance(requested, str) or requested.strip().lower() not in EFFORT_LEVELS:
        return None
    effort = requested.strip().lower()
    cap = config.get(PROJECT_ROLE_MAX_EFFORT_KEY) or config.get("effort")
    if not isinstance(cap, str) or cap.strip().lower() not in EFFORT_LEVELS:
        cap = DEFAULT_PROJECT_ROLE_MAX_EFFORT
    cap = cap.strip().lower()
    if EFFORT_LEVELS.index(effort) > EFFORT_LEVELS.index(cap):
        logger.warning("project role %s requested effort=%s; capped at %s",
                       role, effort, cap)
        return cap
    return effort


def _allowed_overlay_exemption(role: str, requested: object, config: dict) -> bool | None:
    if not isinstance(requested, bool):
        return None
    allowlist = config.get(EARLY_TERM_EXEMPT_PROJECT_ROLES_KEY) or []
    if not isinstance(allowlist, list):
        allowlist = []
    if role in allowlist:
        return requested
    if requested:
        logger.warning(
            "project role %s requests early_term_exempt but is not in the "
            "operator's %s dispatch-config allowlist; not exempt",
            role, EARLY_TERM_EXEMPT_PROJECT_ROLES_KEY)
    return None


def _build_role_config(
    role: str, path: Path, text: str, *, is_project_role: bool,
) -> RoleConfig:
    meta, body = parse_frontmatter(text)
    turns = meta.get("turns")
    effort = meta.get("effort")
    exempt = meta.get("early_term_exempt")
    skills = meta.get("skills") or []
    if is_project_role:
        config = _operator_config()
        turns = _cap_overlay_turns(role, turns, config)
        effort = _cap_overlay_effort(role, effort, config)
        exempt = _allowed_overlay_exemption(role, exempt, config)
        # SR-2997 S7: overlay-supplied skill paths would be a traversal /
        # injection vector the moment anything consumes them; drop them.
        if skills:
            logger.warning("role overlay %s: 'skills' is not honoured for "
                           "project roles; ignored", role)
        skills = []
    return RoleConfig(
        name=role,
        path=path,
        body=body,
        model=meta.get("model"),
        turns=_positive_int(turns),
        effort=effort,
        early_term_exempt=exempt if isinstance(exempt, bool) else None,
        skills=list(skills),
        is_project_role=is_project_role,
    )


# --- Public resolution API ---------------------------------------------------

def _base_role_file(role: str) -> Path | None:
    """Locate a base role's ``.md`` in ``ROLE_PROMPTS`` or ``prompts/``."""
    base = _equipa_constants.ROLE_PROMPTS.get(role)
    if base and Path(base).is_file():
        return Path(base)
    if not _OVERLAY_ROLE_NAME_RE.fullmatch(role):
        return None
    direct = PROMPTS_DIR / f"{role}.md"
    if direct.is_file():
        return direct
    return None


def resolve_role(role: str, project_dir: str | None = None) -> RoleConfig | None:
    """Resolve ``role`` to a :class:`RoleConfig`.

    A base role always resolves to its shared prompt. A NEW role name
    resolves to the project overlay read from the trusted source (see the
    module docstring). Returns ``None`` when no file exists for the role.
    Reads files fresh per call and shares no mutable role state, so
    concurrent resolution for different projects is safe and same-named
    roles in different projects never interfere.
    """
    overlay = _read_project_overlay(role, project_dir)
    if overlay is not None:
        path, text = overlay
        return _build_role_config(role, path, text, is_project_role=True)
    base = _base_role_file(role)
    if base is None:
        return None
    return _build_role_config(
        role, base, base.read_text(encoding="utf-8"), is_project_role=False,
    )


def role_exists(role: str, project_dir: str | None = None) -> bool:
    """True if ``role`` resolves to a base prompt or a trusted project overlay."""
    if _base_role_file(role) is not None:
        return True
    return _read_project_overlay(role, project_dir) is not None


def is_role_early_term_exempt(role: str, project_dir: str | None = None) -> bool:
    """Whether ``role`` is exempt from the no-file-change early-termination kill.

    A base role file's frontmatter ``early_term_exempt`` (when set) wins; a
    project role's is honoured only when the operator allowlists it.
    Otherwise falls back to the base ``EARLY_TERM_EXEMPT_ROLES`` set.
    """
    rc = resolve_role(role, project_dir)
    if rc is not None and rc.early_term_exempt is not None:
        return rc.early_term_exempt
    return role in EARLY_TERM_EXEMPT_ROLES


def available_roles(project_dir: str | None = None) -> list[str]:
    """All dispatchable role names for ``project_dir``: base + project overlay.

    Base roles come from ``prompts/`` (and the discovered ``ROLE_PROMPTS`` dict);
    project roles are the non-reserved names in the trusted overlay. Used for
    helpful "unknown role" error messages, since argparse cannot enumerate
    project-overlay roles at parse time (the project dir is not known until a
    task is resolved).
    """
    names: set[str] = set()
    if PROMPTS_DIR.is_dir():
        for f in PROMPTS_DIR.glob("*.md"):
            if not f.name.startswith("_"):
                names.add(f.stem)
    names.update(_equipa_constants.ROLE_PROMPTS.keys())
    if project_dir:
        names.update(_overlay_role_names(project_dir))
    return sorted(names)
