"""Generated files a gated merge may regenerate instead of failing (task #3131).

Every parallel ``--tasks`` batch failed to merge its second and later branches
on ``equipa/MODULE_DEPENDENCY_REPORT.md``: each task regenerates the report,
so two task branches always conflict in it, although the file is nothing but
the output of ``scripts/gen_module_report.py``.

:data:`GENERATED_FILES` declares such files and their generators in one
place. When the orchestrator's merge conflicts ONLY in declared files,
:func:`resolve_generated_conflicts` regenerates them from the merged tree,
stages them and completes the merge commit. Any other conflict — or a
project without the file or its generator — keeps the ordinary path (abort,
``merge_failed``, branch preserved).

Security: the generator is code from the merged tree, which agents can write.

* It runs only when the task branch left the generator's blob identical to
  the default-branch SHA the run's guard pinned (and the merged tree carries
  that same blob). The pinned SHA is passed in, never re-read from the
  checkout, and the merge must have started from it (task #3141, I-01). A
  branch that changed the generator gets ``merge_failed`` with that reason;
  its generator is never run.
* It never runs in the orchestrator's process nor in the main checkout: it
  runs as ``python -I`` (no script directory, cwd, user site or ``PYTHON*``
  variable on the import path) inside a private export of its inputs in the
  merged tree, with the scrubbed agent environment minus the Claude CLI
  credential, and a timeout that kills its whole process group. The export is
  written from git objects (``ls-tree`` + ``cat-file``), so no attribute
  (``export-ignore``, ``export-subst``) can drop or rewrite an input, and no
  archive is unpacked (task #3141, I-02/I-04). Output is read in chunks and
  the generator is killed once it exceeds the size cap.
* The resolution is committed only when its tree differs from
  ``git merge-tree``'s merge of the same two commits in the regenerated,
  conflicted paths alone, each holding exactly the blob of the verified
  generator output; a commit that is not that tree is refused (task #3141,
  I-03). :meth:`DefaultBranchGuard.record_merge` re-checks paths and blobs on
  the landed commit (the task #3116 merge-integrity check).

Agents share the orchestrator's UID, so this is not a sandbox; it keeps the
orchestrator from running branch-authored code and keeps the merge exact.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from equipa.env_loader import build_agent_env
from equipa.git_ops import git_run_async
from equipa.merge_integrity import (
    commit_parents,
    merged_tree,
    regenerated_resolution_problem,
    resolve_commit,
    resolve_tree,
)

logger = logging.getLogger(__name__)

# Replaced by the export root in a generator's arguments.
ROOT_PLACEHOLDER = "{root}"


@dataclass(frozen=True)
class GeneratedFile:
    """A repository file that is pure output of a generator script.

    ``path`` and ``generator`` are POSIX paths relative to the work-tree root.
    ``args`` follow the script; the generator must print the file's full new
    content to stdout (it is never allowed to write the file itself).
    ``inputs`` are the work-tree paths (files or directories) the generator
    reads; only they and the generator are exported for it to run in. Empty
    means the whole tree.
    """

    path: str
    generator: str
    args: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()

    def argv(self, root: Path) -> list[str]:
        """The generator command line for an export rooted at ``root``."""
        return [
            sys.executable, "-I", "-B", str(root / self.generator),
            *(arg.replace(ROOT_PLACEHOLDER, str(root)) for arg in self.args),
        ]

    def exports(self, path: str) -> bool:
        """True when the work-tree ``path`` belongs in this generator's export."""
        if path == self.generator or not self.inputs:
            return True
        return any(
            path == prefix or path.startswith(f"{prefix}/") for prefix in self.inputs
        )


# The one place generated files are declared.
GENERATED_FILES: tuple[GeneratedFile, ...] = (
    GeneratedFile(
        path="equipa/MODULE_DEPENDENCY_REPORT.md",
        generator="scripts/gen_module_report.py",
        args=("--repo-root", ROOT_PLACEHOLDER, "--stdout"),
        inputs=("equipa",),
    ),
)

GENERATOR_TIMEOUT_SECONDS = 120
MAX_GENERATED_BYTES = 8 * 1024 * 1024
MAX_GENERATOR_STDERR_BYTES = 1024 * 1024
# The generator's inputs written to the private export, summed blob sizes.
MAX_EXPORT_BYTES = 64 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_EXPORT_TIMEOUT_SECONDS = 120
_GIT_TIMEOUT = 30
# Tree entries the export writes; links and submodules are refused.
_EXPORTABLE_MODES = frozenset({"100644", "100755"})
# The agent env keeps the Claude CLI's credential; a generator needs neither.
_GENERATOR_ENV_EXCLUDED = frozenset({"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"})


def generated_file(path: str) -> GeneratedFile | None:
    """The declaration for repository path ``path``, or None."""
    for spec in GENERATED_FILES:
        if spec.path == path:
            return spec
    return None


@dataclass(frozen=True)
class ConflictResolution:
    """Outcome of :func:`resolve_generated_conflicts`.

    ``applicable`` is False when the conflict is not confined to declared
    generated files whose generator exists on the default branch — the
    caller then keeps its ordinary conflict handling. When it is True and
    ``commit`` is None, the resolution was refused for ``reason`` and the
    caller must abort the merge. ``blobs`` maps each regenerated path to the
    blob SHA of the verified generator output the commit carries.
    """

    applicable: bool
    paths: tuple[str, ...] = ()
    commit: str | None = None
    reason: str = ""
    blobs: Mapping[str, str] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.commit is not None


@dataclass(frozen=True)
class _ExportEntry:
    """One blob of the merged tree written to the generator's export."""

    path: str
    mode: str
    blob: str
    size: int


class _OutputTooLarge(Exception):
    """A generator stream passed its byte cap; the generator is killed."""

    def __init__(self, stream: str, limit: int) -> None:
        super().__init__(f"{stream} exceeds {limit} bytes")
        self.stream = stream
        self.limit = limit


async def unmerged_entries(repo: str | os.PathLike) -> dict[str, set[tuple[int, str]]] | None:
    """{path: {(stage, mode)}} for the index's unmerged entries, or None."""
    result = await git_run_async(
        ["ls-files", "--unmerged", "-z", "--full-name"], repo, timeout=_GIT_TIMEOUT,
    )
    if result.returncode != 0:
        return None
    entries: dict[str, set[tuple[int, str]]] = {}
    for record in result.stdout.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if len(fields) != 3 or not path or not fields[2].isdigit():
            return None
        entries.setdefault(path, set()).add((int(fields[2]), fields[0]))
    return entries


async def _blob(repo: str | os.PathLike, treeish: str, path: str) -> str | None:
    """Blob SHA of ``path`` in ``treeish``, or None when it is not a blob there.

    ``ls-tree`` rather than ``rev-parse <tree>:<path>^{blob}``: after the
    colon git reads everything, suffix included, as the path.
    """
    result = await git_run_async(
        ["ls-tree", "-z", "--full-tree", treeish, "--", path],
        repo, timeout=_GIT_TIMEOUT,
    )
    meta, _, listed = result.stdout.rstrip("\0").partition("\t")
    fields = meta.split()
    if result.returncode != 0 or listed != path or len(fields) != 3:
        return None
    return fields[2] if fields[1] == "blob" else None


async def _generator_mismatch_reason(
    repo: str,
    ours: str,
    theirs: str,
    generator: str,
    on_branch: str | None,
    expected: str,
) -> str:
    """Refusal text for a generator whose blob differs between the two sides.

    Either way the generator is not run. The merge base tells which side
    changed it: an operator must not read a generator updated on the default
    branch after the task branched as the agent tampering with it.
    """
    detail = (
        f"(task branch blob {(on_branch or 'deleted')[:12]}, default branch "
        f"{expected[:12]}); it is not run"
    )
    base = await git_run_async(
        ["merge-base", ours, theirs], repo, timeout=_GIT_TIMEOUT,
    )
    base_sha = base.stdout.strip()
    if (
        on_branch is not None
        and base.returncode == 0
        and base_sha
        and await _blob(repo, base_sha, generator) == on_branch
    ):
        return (
            f"the default branch changed the generator {generator} after the "
            f"task branched {detail}"
        )
    return f"the task branch changed the generator {generator} {detail}"


def _content_conflict_problem(path: str, entries: set[tuple[int, str]]) -> str | None:
    """Why ``path`` is not a both-sides-modified regular-file conflict."""
    stages = {stage for stage, _mode in entries}
    if not {2, 3} <= stages:
        return f"{path} is not changed on both sides (stages {sorted(stages)})"
    if any(mode != "100644" for _stage, mode in entries):
        return f"{path} is not a regular file on every side"
    return None


def _unsafe_export_path(path: str) -> bool:
    """True when ``path`` could leave the export or confuse ``--stdin-paths``."""
    return (
        not path
        or path.startswith("/")
        or "\n" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
    )


async def _export_entries(
    repo: str, tree: str, specs: list[GeneratedFile],
) -> list[_ExportEntry] | str:
    """The blobs of ``tree`` the generators read, or why they cannot be exported.

    Read from the object store, so neither ``.gitattributes`` in the tree
    nor ``$GIT_DIR/info/attributes`` can hide or rewrite an input (I-02).
    """
    listed = await git_run_async(
        ["ls-tree", "-r", "-z", "-l", "--full-tree", tree],
        repo, timeout=_EXPORT_TIMEOUT_SECONDS, text=False,
    )
    if listed.returncode != 0:
        detail = listed.stderr.decode("utf-8", "replace").strip()[:200]
        return f"git ls-tree of the merged tree failed: {detail}"
    entries: list[_ExportEntry] = []
    total = 0
    for record in listed.stdout.split(b"\0"):
        if not record:
            continue
        meta, separator, raw_path = record.partition(b"\t")
        fields = meta.decode("ascii", "replace").split()
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            return f"the merged tree has a path that is not UTF-8: {raw_path[:80]!r}"
        if not separator or len(fields) != 4:
            return f"unreadable ls-tree entry for {path[:80]!r}"
        if not any(spec.exports(path) for spec in specs):
            continue
        mode, kind, blob, size = fields
        if kind != "blob" or mode not in _EXPORTABLE_MODES or not size.isdigit():
            # N-01 (task #3146): the path is branch-authored and this text
            # reaches GATE-AUDIT, so it is always quoted (no raw newline).
            return (
                f"the merged tree's {path[:80]!r} is not a regular file "
                f"(mode {mode}); the generator's inputs are not exported"
            )
        if _unsafe_export_path(path):
            return f"the merged tree's path {path[:80]!r} cannot be exported safely"
        total += int(size)
        if total > MAX_EXPORT_BYTES:
            return (
                f"the generator's inputs in the merged tree exceed "
                f"{MAX_EXPORT_BYTES} bytes"
            )
        entries.append(_ExportEntry(path, mode, blob, int(size)))
    return entries


def _write_export(root: Path, entries: list[_ExportEntry], batch: bytes) -> str | None:
    """Write each entry's content from ``git cat-file --batch`` output under ``root``.

    ``root`` is a fresh private directory, every file is created exclusively
    and never through a link. Returns a problem, or None.
    """
    offset = 0
    for entry in entries:
        header_end = batch.find(b"\n", offset)
        header = batch[offset:header_end].decode("ascii", "replace").split()
        start = header_end + 1
        end = start + entry.size
        if (
            header_end < 0
            or header != [entry.blob, "blob", str(entry.size)]
            or batch[end:end + 1] != b"\n"
        ):
            return f"git cat-file did not return the blob of {entry.path}"
        target = root.joinpath(*entry.path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o755 if entry.mode == "100755" else 0o644,
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(batch[start:end])
        offset = end + 1
    if offset != len(batch):
        return "git cat-file returned more objects than requested"
    return None


async def _export_from_objects(
    repo: str, tree: str, specs: list[GeneratedFile], root: Path,
) -> str | None:
    """Write the generators' inputs in ``tree`` to ``root``; a problem, or None.

    After writing, every exported file is hashed with ``--no-filters`` and
    must be exactly the blob ``ls-tree`` listed for it.
    """
    entries = await _export_entries(repo, tree, specs)
    if isinstance(entries, str):
        return entries
    request = "".join(f"{entry.blob}\n" for entry in entries).encode("ascii")
    batch = await git_run_async(
        ["cat-file", "--batch"], repo, timeout=_EXPORT_TIMEOUT_SECONDS,
        input=request, text=False,
    )
    if batch.returncode != 0:
        detail = batch.stderr.decode("utf-8", "replace").strip()[:200]
        return f"git cat-file of the merged tree failed: {detail}"
    root.mkdir(mode=0o700)
    try:
        problem = await asyncio.to_thread(_write_export, root, entries, batch.stdout)
    except OSError as exc:
        return f"merged tree could not be exported: {exc}"
    if problem:
        return problem
    if not entries:
        return None
    paths = "".join(
        f"{root.joinpath(*entry.path.split('/'))}\n" for entry in entries
    )
    hashed = await git_run_async(
        ["hash-object", "--no-filters", "--stdin-paths"], repo,
        timeout=_EXPORT_TIMEOUT_SECONDS, input=os.fsencode(paths),
    )
    if hashed.returncode != 0 or hashed.stdout.split() != [
        entry.blob for entry in entries
    ]:
        return "the export does not match the merged tree's blobs"
    return None


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the generator's process group and reap it, bounded."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        logger.error(
            "[Generated-Files] generator pid %s did not exit after SIGKILL", proc.pid,
        )


def generator_env() -> dict[str, str]:
    """Scrubbed environment for a generator: the agent env minus the CLI's."""
    return {
        name: value for name, value in build_agent_env().items()
        if name not in _GENERATOR_ENV_EXCLUDED
    }


async def _read_capped(stream: asyncio.StreamReader, limit: int, name: str) -> bytes:
    """Read ``stream`` to EOF; raise :class:`_OutputTooLarge` past ``limit``."""
    buffer = bytearray()
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            return bytes(buffer)
        if len(buffer) + len(chunk) > limit:
            raise _OutputTooLarge(name, limit)
        buffer += chunk


async def _collect_output(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """The generator's stdout and stderr, each capped while it streams (I-04)."""
    readers = [
        asyncio.ensure_future(
            _read_capped(proc.stdout, MAX_GENERATED_BYTES, "output"),
        ),
        asyncio.ensure_future(
            _read_capped(proc.stderr, MAX_GENERATOR_STDERR_BYTES, "stderr"),
        ),
    ]
    try:
        stdout, stderr = await asyncio.gather(*readers)
    finally:
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
    await proc.wait()
    return stdout, stderr


async def run_generator(
    spec: GeneratedFile, root: Path, *, timeout: float | None = None,
) -> bytes | str:
    """Run ``spec``'s generator in the export ``root``; its stdout, or a problem."""
    limit = GENERATOR_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        proc = await asyncio.create_subprocess_exec(
            *spec.argv(root),
            cwd=str(root),
            env=generator_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return f"generator {spec.generator} could not start: {exc}"
    try:
        stdout, stderr = await asyncio.wait_for(_collect_output(proc), timeout=limit)
    except asyncio.TimeoutError:
        await _kill_process_group(proc)
        return f"generator {spec.generator} timed out after {limit}s"
    except _OutputTooLarge as exc:
        await _kill_process_group(proc)
        return f"generator {spec.generator} {exc}"
    if proc.returncode != 0:
        detail = " ".join(stderr.decode("utf-8", "replace").split())[-300:]
        return f"generator {spec.generator} exited {proc.returncode}: {detail}"
    if not stdout:
        return f"generator {spec.generator} produced no output"
    return stdout


async def _regenerate(
    repo: str, tree: str, specs: list[GeneratedFile], generator_blobs: dict[str, str],
) -> dict[str, bytes] | str:
    """Run each generator on a private export of ``tree``: {path: content}."""
    workdir = Path(tempfile.mkdtemp(prefix="equipa-regen-"))
    try:
        root = workdir / "tree"
        problem = await _export_from_objects(repo, tree, specs, root)
        if problem:
            return problem
        outputs: dict[str, bytes] = {}
        for spec in specs:
            # The bytes that run must be the pinned default branch's blob.
            hashed = await git_run_async(
                ["hash-object", "--no-filters", "--", str(root / spec.generator)],
                repo, timeout=_GIT_TIMEOUT,
            )
            if hashed.returncode != 0 or hashed.stdout.strip() != generator_blobs[spec.generator]:
                return f"exported {spec.generator} is not the default branch's blob"
            produced = await run_generator(spec, root)
            if isinstance(produced, str):
                return produced
            outputs[spec.path] = produced
        return outputs
    finally:
        await asyncio.to_thread(shutil.rmtree, workdir, True)


async def _output_blobs(repo: str, outputs: dict[str, bytes]) -> dict[str, str] | str:
    """Blob SHA of each regenerated file's exact bytes: {path: blob}."""
    blobs: dict[str, str] = {}
    for path, content in outputs.items():
        hashed = await git_run_async(
            ["hash-object", "--no-filters", "--stdin"], repo,
            timeout=_GIT_TIMEOUT, input=content,
        )
        blob = hashed.stdout.strip()
        if hashed.returncode != 0 or not blob:
            return f"git hash-object of the regenerated {path} failed"
        blobs[path] = blob
    return blobs


def _write_checkout_file(repo: str, rel_path: str, content: bytes) -> None:
    """Overwrite the conflicted ``rel_path`` in the checkout, never via a link."""
    root = Path(os.path.realpath(repo))
    target = root / rel_path
    expected_parent = root.joinpath(*Path(rel_path).parent.parts)
    if Path(os.path.realpath(target.parent)) != expected_parent:
        raise OSError(f"{rel_path}: parent directory resolves outside {root}")
    fd = os.open(target, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "wb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"{rel_path} is not a regular file")
        handle.write(content)


async def resolve_generated_conflicts(
    repo: str | os.PathLike,
    *,
    ours: str,
    theirs: str,
    head_before_merge: str,
    message: str,
    default_branch: str | None = None,
) -> ConflictResolution:
    """Complete the in-progress conflicted merge of ``theirs`` into ``ours``.

    ``repo`` is the main checkout at the work-tree root, mid-merge: ``ours``
    is the default-branch SHA the run's guard pinned, ``theirs`` the approved
    commit being merged and ``head_before_merge`` the checkout's HEAD read
    just before ``git merge``. Applies only when every unmerged path is a
    declared generated file whose generator exists at ``ours``; then either
    commits the resolution (``commit`` set) or refuses with a ``reason`` and
    leaves the aborting to the caller. Never raises for git or generator
    failures.

    Task #3141 (I-01): the generator is trusted only as the blob at the
    pinned ``ours``. A merge that did not start from ``ours``, or a HEAD that
    moved away from it, is refused before anything runs.

    Task #3146 (N-02): the resolution is built with ``commit-tree`` and the
    branch HEAD names (``default_branch`` when given) is moved by a
    compare-and-swap ``update-ref`` whose old value is ``ours``. A branch
    that moved after the checks is never committed on; it is left untouched
    and the resolution is refused.
    """
    repo = os.fspath(repo)
    conflicts = await unmerged_entries(repo)
    if not conflicts:
        return ConflictResolution(False, reason="no readable unmerged paths")
    specs: list[GeneratedFile] = []
    for path in sorted(conflicts):
        spec = generated_file(path)
        if spec is None:
            return ConflictResolution(
                False, reason=f"{path[:80]!r} is not a generated file",
            )
        specs.append(spec)
    generator_blobs: dict[str, str] = {}
    for spec in specs:
        blob = await _blob(repo, ours, spec.generator)
        if blob is None:
            return ConflictResolution(
                False, reason=f"{spec.generator} is not on the default branch",
            )
        generator_blobs[spec.generator] = blob

    paths = tuple(spec.path for spec in specs)

    def refuse(reason: str) -> ConflictResolution:
        return ConflictResolution(True, paths, None, reason)

    if head_before_merge != ours:
        return refuse(
            f"the merge started from {head_before_merge[:12] or 'unknown'}, not "
            f"the pinned default-branch SHA {ours[:12]}: the default branch "
            f"moved, so no generator is run"
        )
    head_now = await resolve_commit(repo, "HEAD")
    if head_now != ours:
        return refuse(
            f"HEAD is {(head_now or 'unresolved')[:12]}, not the pinned "
            f"default-branch SHA {ours[:12]}: the default branch moved, so no "
            f"generator is run"
        )
    for path in paths:
        problem = _content_conflict_problem(path, conflicts[path])
        if problem:
            return refuse(problem)
    if await resolve_commit(repo, "MERGE_HEAD") != theirs:
        return refuse(f"MERGE_HEAD is not the approved commit {theirs[:12]}")
    for generator, expected in generator_blobs.items():
        on_branch = await _blob(repo, theirs, generator)
        if on_branch != expected:
            return refuse(await _generator_mismatch_reason(
                repo, ours, theirs, generator, on_branch, expected,
            ))
    merged = await merged_tree(repo, ours, theirs)
    if merged is None or merged.conflicted != frozenset(paths):
        return refuse("git merge-tree does not reproduce the checkout's conflict")
    for generator, expected in generator_blobs.items():
        if await _blob(repo, merged.tree, generator) != expected:
            return refuse(f"the merged tree's {generator} is not the default branch's")

    outputs = await _regenerate(repo, merged.tree, specs, generator_blobs)
    if isinstance(outputs, str):
        return refuse(outputs)
    blobs = await _output_blobs(repo, outputs)
    if isinstance(blobs, str):
        return refuse(blobs)
    try:
        for path in paths:
            _write_checkout_file(repo, path, outputs[path])
    except OSError as exc:
        return refuse(f"regenerated file could not be written: {exc}")
    staged = await git_run_async(
        ["add", "--", *paths], repo, timeout=_GIT_TIMEOUT,
    )
    if staged.returncode != 0:
        return refuse(f"git add failed: {staged.stderr.strip()[:200]}")
    if await unmerged_entries(repo) != {}:
        return refuse("unmerged paths remain after regeneration")
    written = await git_run_async(["write-tree"], repo, timeout=_GIT_TIMEOUT)
    staged_tree = written.stdout.strip()
    if written.returncode != 0 or not staged_tree:
        return refuse(f"git write-tree failed: {written.stderr.strip()[:200]}")
    problem = await regenerated_resolution_problem(
        repo, merged, staged_tree, frozenset(paths), blobs,
    )
    if problem:
        return refuse(problem)
    branch_ref = await _checkout_branch_ref(repo, default_branch)
    if branch_ref is None:
        return refuse(
            "the checkout's HEAD is not the default branch, so the resolution "
            "is not committed"
        )
    # N-02 (task #3146): the resolution commit is built from the verified
    # tree with the exact parents, and the branch moves only by a
    # compare-and-swap from the pinned SHA. If the default branch moved at
    # any point after the HEAD check, nothing is committed on top of it and
    # it is left exactly where it is.
    built = await git_run_async(
        ["commit-tree", staged_tree, "-p", ours, "-p", theirs, "-F", "-"],
        repo, timeout=60, input=message.encode("utf-8"),
    )
    head = built.stdout.strip()
    if built.returncode != 0 or not head:
        return refuse(f"git commit-tree failed: {built.stderr.strip()[:200]}")
    # I-03: the commit must be exactly the verified resolution.
    if await commit_parents(repo, head) != [ours, theirs]:
        return refuse(
            f"resolution commit {head[:12]} is not a merge of the pinned "
            f"default branch {ours[:12]} and the approved commit {theirs[:12]}"
        )
    if await resolve_tree(repo, head) != staged_tree:
        return refuse(
            f"resolution commit {head[:12]} is not the verified tree "
            f"{staged_tree[:12]}"
        )
    # The checkout must match what lands, or a later commit there would
    # carry whatever was swapped into the index.
    rewritten = await git_run_async(["write-tree"], repo, timeout=_GIT_TIMEOUT)
    if rewritten.returncode != 0 or rewritten.stdout.strip() != staged_tree:
        # Put the verified tree back so the caller's ``merge --abort`` can
        # return the checkout to the default branch: it refuses while an
        # index entry differs from both HEAD and the work tree.
        await git_run_async(["read-tree", staged_tree], repo, timeout=_GIT_TIMEOUT)
        await git_run_async(
            ["update-index", "-q", "--refresh"], repo, timeout=_GIT_TIMEOUT,
        )
        return refuse(
            f"the checkout's index is not the verified tree {staged_tree[:12]} "
            f"any more"
        )
    swapped = await git_run_async(
        ["update-ref", "-m", f"merge (regenerated): {message.splitlines()[0]}",
         branch_ref, head, ours],
        repo, timeout=_GIT_TIMEOUT,
    )
    if swapped.returncode != 0:
        return refuse(
            f"{branch_ref} is no longer the pinned default-branch SHA "
            f"{ours[:12]} (compare-and-swap refused): the default branch "
            f"moved, so the resolution {head[:12]} is not committed"
        )
    # HEAD, index and work tree now agree; only the merge state is left.
    quit_merge = await git_run_async(
        ["merge", "--quit"], repo, timeout=_GIT_TIMEOUT,
    )
    if quit_merge.returncode != 0:
        logger.warning(
            "[Generated-Files] merge --quit after the resolution failed: %s",
            quit_merge.stderr.strip()[:200],
        )
    return ConflictResolution(True, paths, head, "regenerated", blobs)


async def _checkout_branch_ref(repo: str, default_branch: str | None) -> str | None:
    """The branch ref HEAD names, or None when HEAD is not that branch.

    ``default_branch`` (when given) must be the branch HEAD is on; a
    detached HEAD or any ref outside ``refs/heads/`` is refused.
    """
    symbolic = await git_run_async(
        ["symbolic-ref", "-q", "HEAD"], repo, timeout=_GIT_TIMEOUT,
    )
    ref = symbolic.stdout.strip()
    if symbolic.returncode != 0 or not ref.startswith("refs/heads/"):
        return None
    if default_branch is not None and ref != f"refs/heads/{default_branch}":
        return None
    return ref
