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
  the pinned default branch (and the merged tree carries that same blob).
  A branch that changed the generator gets ``merge_failed`` with that reason;
  its generator is never run.
* It never runs in the orchestrator's process nor in the main checkout: it
  runs as ``python -I`` (no script directory, cwd, user site or ``PYTHON*``
  variable on the import path) inside a private export of the merged tree,
  with the scrubbed agent environment minus the Claude CLI credential, and a
  timeout that kills its whole process group.
* The resolution is committed only when its tree differs from
  ``git merge-tree``'s merge of the same two commits in the regenerated,
  conflicted paths alone; :meth:`DefaultBranchGuard.record_merge` re-checks
  that on the landed commit (the task #3116 merge-integrity check).

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
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from equipa.env_loader import build_agent_env
from equipa.git_ops import git_run_async
from equipa.merge_integrity import (
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
    """

    path: str
    generator: str
    args: tuple[str, ...] = ()

    def argv(self, root: Path) -> list[str]:
        """The generator command line for an export rooted at ``root``."""
        return [
            sys.executable, "-I", "-B", str(root / self.generator),
            *(arg.replace(ROOT_PLACEHOLDER, str(root)) for arg in self.args),
        ]


# The one place generated files are declared.
GENERATED_FILES: tuple[GeneratedFile, ...] = (
    GeneratedFile(
        path="equipa/MODULE_DEPENDENCY_REPORT.md",
        generator="scripts/gen_module_report.py",
        args=("--repo-root", ROOT_PLACEHOLDER, "--stdout"),
    ),
)

GENERATOR_TIMEOUT_SECONDS = 120
MAX_GENERATED_BYTES = 8 * 1024 * 1024
_EXPORT_TIMEOUT_SECONDS = 120
_GIT_TIMEOUT = 30
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
    caller must abort the merge.
    """

    applicable: bool
    paths: tuple[str, ...] = ()
    commit: str | None = None
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.commit is not None


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
    """Blob SHA of ``path`` in ``treeish``, or None when it is not a blob there."""
    result = await git_run_async(
        ["rev-parse", "--verify", "--quiet", "--end-of-options",
         f"{treeish}:{path}^{{blob}}"],
        repo, timeout=_GIT_TIMEOUT,
    )
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def _content_conflict_problem(path: str, entries: set[tuple[int, str]]) -> str | None:
    """Why ``path`` is not a both-sides-modified regular-file conflict."""
    stages = {stage for stage, _mode in entries}
    if not {2, 3} <= stages:
        return f"{path} is not changed on both sides (stages {sorted(stages)})"
    if any(mode != "100644" for _stage, mode in entries):
        return f"{path} is not a regular file on every side"
    return None


def _extract_export(archive: Path, root: Path) -> None:
    """Unpack the merged-tree tarball; the ``data`` filter refuses links out."""
    root.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(root, filter="data")


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
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=limit)
    except asyncio.TimeoutError:
        await _kill_process_group(proc)
        return f"generator {spec.generator} timed out after {limit}s"
    if proc.returncode != 0:
        detail = " ".join(stderr.decode("utf-8", "replace").split())[-300:]
        return f"generator {spec.generator} exited {proc.returncode}: {detail}"
    if not stdout:
        return f"generator {spec.generator} produced no output"
    if len(stdout) > MAX_GENERATED_BYTES:
        return f"generator {spec.generator} output exceeds {MAX_GENERATED_BYTES} bytes"
    return stdout


async def _regenerate(
    repo: str, tree: str, specs: list[GeneratedFile], generator_blobs: dict[str, str],
) -> dict[str, bytes] | str:
    """Run each generator on a private export of ``tree``: {path: content}."""
    workdir = Path(tempfile.mkdtemp(prefix="equipa-regen-"))
    try:
        archive = workdir / "merged.tar"
        root = workdir / "tree"
        exported = await git_run_async(
            ["archive", "--format=tar", f"--output={archive}", tree],
            repo, timeout=_EXPORT_TIMEOUT_SECONDS,
        )
        if exported.returncode != 0:
            return f"git archive of the merged tree failed: {exported.stderr.strip()[:200]}"
        try:
            await asyncio.to_thread(_extract_export, archive, root)
        except (tarfile.TarError, OSError) as exc:
            return f"merged tree could not be exported: {exc}"
        outputs: dict[str, bytes] = {}
        for spec in specs:
            # The bytes that run must be the default branch's blob, whatever
            # attributes in the merged tree did to the export.
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
    repo: str | os.PathLike, *, ours: str, theirs: str, message: str,
) -> ConflictResolution:
    """Complete the in-progress conflicted merge of ``theirs`` into ``ours``.

    ``repo`` is the main checkout at the work-tree root, mid-merge: ``ours``
    is the default-branch SHA pinned before the merge, ``theirs`` the
    approved commit being merged. Applies only when every unmerged path is a
    declared generated file whose generator exists at ``ours``; then either
    commits the resolution (``commit`` set) or refuses with a ``reason`` and
    leaves the aborting to the caller. Never raises for git or generator
    failures.
    """
    repo = os.fspath(repo)
    conflicts = await unmerged_entries(repo)
    if not conflicts:
        return ConflictResolution(False, reason="no readable unmerged paths")
    specs: list[GeneratedFile] = []
    for path in sorted(conflicts):
        spec = generated_file(path)
        if spec is None:
            return ConflictResolution(False, reason=f"{path} is not a generated file")
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

    for path in paths:
        problem = _content_conflict_problem(path, conflicts[path])
        if problem:
            return refuse(problem)
    if await resolve_commit(repo, "MERGE_HEAD") != theirs:
        return refuse(f"MERGE_HEAD is not the approved commit {theirs[:12]}")
    for generator, expected in generator_blobs.items():
        on_branch = await _blob(repo, theirs, generator)
        if on_branch != expected:
            return refuse(
                f"the task branch changed the generator {generator} (blob "
                f"{(on_branch or 'deleted')[:12]}, default branch "
                f"{expected[:12]}); it is not run"
            )
    merged = await merged_tree(repo, ours, theirs)
    if merged is None or merged.conflicted != frozenset(paths):
        return refuse("git merge-tree does not reproduce the checkout's conflict")
    for generator, expected in generator_blobs.items():
        if await _blob(repo, merged.tree, generator) != expected:
            return refuse(f"the merged tree's {generator} is not the default branch's")

    outputs = await _regenerate(repo, merged.tree, specs, generator_blobs)
    if isinstance(outputs, str):
        return refuse(outputs)
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
        repo, merged, staged_tree, frozenset(paths),
    )
    if problem:
        return refuse(problem)
    committed = await git_run_async(
        ["commit", "--no-verify", "--quiet", "-m", message], repo, timeout=60,
    )
    if committed.returncode != 0:
        return refuse(f"git commit failed: {committed.stderr.strip()[:200]}")
    head = await resolve_commit(repo, "HEAD")
    if head is None or head == ours:
        return refuse("git commit did not advance the default branch")
    if await resolve_tree(repo, head) != staged_tree:
        # The commit landed but is not the verified tree: report it; the
        # run's DefaultBranchGuard refuses to record it and raises the alarm.
        logger.error(
            "[Generated-Files] resolution commit %s is not the verified tree %s",
            head[:12], staged_tree[:12],
        )
    return ConflictResolution(True, paths, head, "regenerated")
