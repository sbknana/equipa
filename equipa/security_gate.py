"""Security-gate helpers — task #2360 + task #2451.

This module owns the small, pure decisions the parallel and single-task
dispatch paths need to make about the security-review gate:

  * ``is_doc_only_diff(changed_files)`` — True when every changed file is
    a documentation file (.md/.txt/.rst/...). Such diffs cannot introduce
    code-level vulnerabilities, so the security gate must not block them
    on prose-matching false positives. Concrete trigger: task #2358, a
    pure CRYPTOTRADER-V3-ARCHITECTURE.md spec, was blocked because the
    document used the word "HIGH" and discussed API-key auth.

  * ``SecurityGateBypassError`` — the defensive invariant raised by
    ``dispatch._merge_task_branch`` when its artifact re-check trips.

  * ``format_counts`` / ``_gate_audit_log`` — shared helpers for the
    [GATE-AUDIT] telemetry line both dispatch paths emit.

Task #2451 Phase J (F-03 fix): an earlier draft also exposed
``evaluate_gate`` + ``GateVerdict`` — a second open-coded gate policy
that lived in parallel with the one in ``dispatch._gated_merge_task``.
That duplication was the same architectural shape as the original
parent bug (decoupled single-task and parallel gates that drifted out
of sync), so it has been deleted. The gate policy now lives in EXACTLY
one place: ``dispatch._gated_merge_task`` + the defensive invariant in
``dispatch._merge_task_branch``. Future callers needing the gate decision
must go through that one path.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from equipa.git_ops import git_run_async

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Task #3041 — reviewer-run provenance.
#
# Observed on CryptoTrader #3035: the security reviewer timed out, the
# orchestrator then copied the DEVELOPER's self-review (committed on the task
# branch at the reviewer's artifact path) to the stable path, and the gate
# parsed it as the reviewer's verdict and merged. The gate must only trust an
# artifact written BY the reviewer run of this cycle. The trust anchor is an
# in-process record the agents cannot reach:
#
#   * a nonce minted when each reviewer attempt starts. It appears ONLY in
#     that attempt's prompt, so an artifact carrying it was written by that
#     run — never by the developer, whose session ended before it existed;
#   * the artifact fingerprint (path, sha256, mtime) before the run started
#     and after it finished, so a pre-existing file or one edited after the
#     review is rejected and every gate line names the exact bytes it parsed;
#   * the run status. A failed / timed-out / crashed run blocks the merge
#     regardless of any artifact on disk.
# ---------------------------------------------------------------------------

REVIEWER_STATUS_RUNNING = "running"
REVIEWER_STATUS_SUCCEEDED = "succeeded"
REVIEWER_STATUS_FAILED = "failed"
REVIEWER_STATUS_SKIPPED_DOC_ONLY = "skipped-doc-only"

# Provenance reasons that mean "no usable reviewer run happened", logged as
# GATE-AUDIT event=reviewer-failed. Every other untrusted reason is an
# artifact problem, logged as event=artifact-provenance-rejected.
_REVIEWER_FAILED_REASONS: frozenset[str] = frozenset({
    f"reviewer-{REVIEWER_STATUS_FAILED}",
    f"reviewer-{REVIEWER_STATUS_RUNNING}",
})

_REVIEWER_NONCE_LINE_TEMPLATE = "<!-- EQUIPA-REVIEWER-RUN: {nonce} -->"
_REVIEWER_NONCE_RE = re.compile(
    r"^[ \t]*<!--[ \t]*EQUIPA-REVIEWER-RUN:[ \t]*([0-9a-f]{32})[ \t]*-->[ \t]*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ArtifactFingerprint:
    """What a review artifact looked like at one instant."""

    path: str
    exists: bool
    sha256: str | None = None
    mtime: float | None = None
    size: int | None = None

    def describe(self) -> str:
        """Render as ``artifact=<path> sha256=<16 hex> mtime=<ts> size=<n>``."""
        if not self.exists:
            return f"artifact={self.path} exists=False"
        return (
            f"artifact={self.path} sha256={(self.sha256 or '')[:16]} "
            f"mtime={self.mtime:.3f} size={self.size}"
        )


def fingerprint_artifact(path: str | os.PathLike) -> ArtifactFingerprint:
    """Hash ``path`` and capture its mtime; ``exists=False`` when unreadable."""
    artifact = Path(path)
    try:
        data = artifact.read_bytes()
        mtime = artifact.stat().st_mtime
    except FileNotFoundError:
        return ArtifactFingerprint(path=str(artifact), exists=False)
    except OSError as exc:
        # A directory or permission error at the artifact path is not a
        # review — report it as absent so every caller fails closed.
        logger.warning("[security-gate] cannot read artifact %s: %s", artifact, exc)
        return ArtifactFingerprint(path=str(artifact), exists=False)
    return ArtifactFingerprint(
        path=str(artifact),
        exists=True,
        sha256=hashlib.sha256(data).hexdigest(),
        mtime=mtime,
        size=len(data),
    )


@dataclass(frozen=True)
class ReviewerRunRecord:
    """The orchestrator's own record of one task's security-reviewer run."""

    task_id: int | str
    nonce: str
    status: str
    started_at: float
    pre_artifact: ArtifactFingerprint | None = None
    post_artifact: ArtifactFingerprint | None = None
    attempts: int = 0
    duration: float = 0.0
    timeouts: tuple[int, ...] = ()
    failure_reason: str | None = None


@dataclass(frozen=True)
class ProvenanceVerdict:
    """Whether the gate may believe the artifact at ``fingerprint.path``."""

    trusted: bool
    reason: str
    fingerprint: ArtifactFingerprint

    @property
    def audit_event(self) -> str:
        """GATE-AUDIT event name for an untrusted verdict."""
        if self.reason in _REVIEWER_FAILED_REASONS:
            return "reviewer-failed"
        return "artifact-provenance-rejected"


_REVIEWER_RUNS: dict[str, ReviewerRunRecord] = {}
_REVIEWER_RUNS_LOCK = threading.Lock()


def new_reviewer_nonce() -> str:
    """Mint a fresh 128-bit reviewer-run nonce (32 lowercase hex chars)."""
    return secrets.token_hex(16)


def reviewer_nonce_line(nonce: str) -> str:
    """The exact line a reviewer run must write into its artifact."""
    return _REVIEWER_NONCE_LINE_TEMPLATE.format(nonce=nonce)


def artifact_nonces(text: str) -> set[str]:
    """Every reviewer-run nonce line present in ``text``."""
    return set(_REVIEWER_NONCE_RE.findall(text or ""))


def record_reviewer_run(record: ReviewerRunRecord) -> None:
    """Store ``record`` as the current reviewer run for its task."""
    with _REVIEWER_RUNS_LOCK:
        _REVIEWER_RUNS[str(record.task_id)] = record


def get_reviewer_run(task_id: int | str) -> ReviewerRunRecord | None:
    """The current reviewer run recorded for ``task_id`` in this process."""
    with _REVIEWER_RUNS_LOCK:
        return _REVIEWER_RUNS.get(str(task_id))


def clear_reviewer_runs() -> None:
    """Forget every recorded reviewer run (test isolation)."""
    with _REVIEWER_RUNS_LOCK:
        _REVIEWER_RUNS.clear()


def verify_reviewer_provenance(
    task_id: int | str, review_path: str | os.PathLike,
) -> ProvenanceVerdict:
    """Decide whether the artifact at ``review_path`` is this cycle's review.

    Untrusted (the merge must block, whatever ``block_on_missing`` says):

      * ``reviewer-failed`` / ``reviewer-running`` — the run failed, timed
        out or crashed before finishing;
      * ``reviewer-skipped-doc-only`` — the caller skipped the reviewer as
        doc-only, yet the gate is evaluating a code diff;
      * ``artifact-missing`` / ``artifact-not-written-by-reviewer`` — nothing
        on disk, or the reviewer left nothing at its path when it finished;
      * ``artifact-pre-existing`` — the bytes equal the file that was there
        before the reviewer started (e.g. a developer self-review);
      * ``reviewer-nonce-missing`` — the file lacks this run's nonce line;
      * ``artifact-changed-after-review`` — edited after the reviewer ended.

    When no run was recorded for the task in this process the verdict is
    trusted with reason ``no-reviewer-run-recorded``: both production gate
    paths run (or explicitly skip) the reviewer in-process first, so this
    only covers direct gate callers, which keep the pre-#3041 artifact-only
    behaviour. The reason is logged so such a gate evaluation is visible.
    """
    fingerprint = fingerprint_artifact(review_path)
    record = get_reviewer_run(task_id)
    if record is None:
        return ProvenanceVerdict(True, "no-reviewer-run-recorded", fingerprint)
    if record.status != REVIEWER_STATUS_SUCCEEDED:
        return ProvenanceVerdict(False, f"reviewer-{record.status}", fingerprint)
    if not fingerprint.exists:
        return ProvenanceVerdict(False, "artifact-missing", fingerprint)
    post = record.post_artifact
    if post is None or not post.exists:
        return ProvenanceVerdict(
            False, "artifact-not-written-by-reviewer", fingerprint,
        )
    pre = record.pre_artifact
    if pre is not None and pre.exists and pre.sha256 == fingerprint.sha256:
        return ProvenanceVerdict(False, "artifact-pre-existing", fingerprint)
    try:
        text = Path(review_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("[security-gate] cannot read artifact %s: %s", review_path, exc)
        return ProvenanceVerdict(False, "artifact-missing", fingerprint)
    if record.nonce not in artifact_nonces(text):
        return ProvenanceVerdict(False, "reviewer-nonce-missing", fingerprint)
    if post.sha256 != fingerprint.sha256:
        return ProvenanceVerdict(
            False, "artifact-changed-after-review", fingerprint,
        )
    return ProvenanceVerdict(True, "verified", fingerprint)


def record_reviewer_skipped_doc_only(task_id: int | str) -> None:
    """Record that the caller skipped the reviewer for a doc-only diff.

    Replaces any earlier run for the task, so if the gate later evaluates a
    CODE diff for it (the caller's diff and the gate's diff disagree), the
    artifact is rejected rather than trusted from a previous cycle.
    """
    record_reviewer_run(ReviewerRunRecord(
        task_id=task_id,
        nonce="",
        status=REVIEWER_STATUS_SKIPPED_DOC_ONLY,
        started_at=0.0,
    ))


def reviewer_run_failure(task_id: int | str) -> str | None:
    """Failure reason when the task's recorded reviewer run did not succeed.

    ``None`` when the run succeeded, was skipped as doc-only, or none was
    recorded. A run still marked ``running`` (the reviewer raised before
    finishing) reports ``crashed``.
    """
    record = get_reviewer_run(task_id)
    if record is None or record.status in (
        REVIEWER_STATUS_SUCCEEDED, REVIEWER_STATUS_SKIPPED_DOC_ONLY,
    ):
        return None
    if record.status == REVIEWER_STATUS_RUNNING:
        return "crashed"
    return record.failure_reason or record.status


def audit_reviewer_run(record: ReviewerRunRecord) -> None:
    """Emit the GATE-AUDIT line summarising a finished reviewer run.

    ``event=reviewer-failed`` for a failed run (so the block is explained
    before the gate even runs), ``event=reviewer-run`` otherwise. Duration,
    attempts and per-attempt timeouts are always included so reviewer
    timeouts are visible without reading the raw agent log.
    """
    failed = record.status != REVIEWER_STATUS_SUCCEEDED
    event = "reviewer-failed" if failed else "reviewer-run"
    timeouts = ",".join(str(value) for value in record.timeouts) or "-"
    post = record.post_artifact
    line = (
        f"task={record.task_id} event={event} status={record.status} "
        f"attempts={record.attempts} duration={record.duration:.1f}s "
        f"timeouts={timeouts} nonce={record.nonce[:8]} "
        f"{post.describe() if post is not None else 'artifact=unknown'}"
    )
    if failed:
        line += f" reason={record.failure_reason or 'unknown'}"
    _gate_audit_log(line, task_id=record.task_id, event=event)


@dataclass(frozen=True)
class GateDecision:
    """The single, ground-truth merge-gate decision (task #2706).

    Prior to task #2706 the gated merge relied on three interacting
    *caller-supplied* signals — a tri-state ``review_blocks_merge``, an
    ``expect_artifact`` doc-only hint, and the ``outcome`` — whose correct
    combination lived only in prose comments across ``dispatch``/``cli``.
    The ``expect_artifact=False`` doc-only short-circuit skipped the
    fail-closed invariant *entirely*, so a future caller passing it wrongly
    would silently disable the last line of defence — exactly the
    caller-trust hole the invariant was built to close.

    A ``GateDecision`` is now computed by :func:`decide_merge_gate` from
    GROUND TRUTH INSIDE ``dispatch._gated_merge_task``:

      * ``changed_files`` — the ACTUAL branch diff (``git diff base...branch``),
        never a caller flag.
      * ``doc_only`` — re-derived by calling :func:`is_doc_only_diff` on that
        real file list (which fails closed on an empty list).
      * ``counts`` / ``blocks_merge`` — read from the on-disk
        ``SECURITY-REVIEW-<task>.md`` artifact, fail-closed on missing.

    The dataclass is frozen so a decision cannot be mutated after it is
    computed. ``expect_artifact`` is an explicit field the gate DERIVES (it
    is False for a provably doc-only diff and when the operator has disabled
    security review outright, True otherwise) so the defensive invariant in
    ``_merge_task_branch`` receives an internally computed value, never a
    caller assertion.
    """

    blocks_merge: bool
    doc_only: bool
    expect_artifact: bool
    counts: dict | None
    reason: str
    changed_files: list[str] = field(default_factory=list)


def role_overlay_changes(changed_files: list[str]) -> list[str]:
    """Return the changed paths that touch a project role overlay.

    Matches any path with a ``.equipa/roles`` component pair (at any depth, so
    a monorepo sub-project is covered) and a bare ``.equipa`` or
    ``.equipa/roles`` entry, which is how a symlink swapping out the overlay
    directory shows up in ``git diff --name-only``.
    """
    touched: list[str] = []
    for raw_path in changed_files:
        # Case-insensitive (SR-2997 S6): never rely on the resolver's own
        # case-sensitive lookup for the gate's safety.
        parts = _path_parts(raw_path)
        for index, part in enumerate(parts):
            if part != ".equipa":
                continue
            is_last = index == len(parts) - 1
            if is_last or parts[index + 1] == "roles":
                touched.append(raw_path)
                break
    return touched


def _path_parts(raw_path: str) -> list[str]:
    """Lower-cased, separator-normalised components of a diff path."""
    return [
        part for part in raw_path.replace("\\", "/").lower().split("/")
        if part not in ("", ".")
    ]


# Basenames Claude Code / other agent CLIs load as standing instructions.
_AGENT_INSTRUCTION_BASENAMES: frozenset[str] = frozenset({"claude.md", "agents.md"})


def agent_config_changes(changed_files: list[str]) -> list[str]:
    """Return the changed paths under a ``.claude`` directory, at any depth.

    SR-2997 S3: ``.claude/skills|agents|commands/*.md`` and
    ``.claude/settings*.json`` are loaded by the Claude CLI for every later
    agent (gate roles included) that runs with the worktree as ``--add-dir``.
    They are agent instructions (and hooks), so only the operator may merge
    them — the gate fails closed on them exactly like ``.equipa/roles/``.
    """
    return [path for path in changed_files if ".claude" in _path_parts(path)]


def is_agent_instruction_path(raw_path: str) -> bool:
    """True for paths an agent CLI reads as instructions, never plain docs.

    Covers any ``.claude`` / ``.equipa`` component and ``CLAUDE.md`` /
    ``AGENTS.md`` at any depth. Such a ``.md`` must never be auto-merged as
    "doc-only" without a security review (SR-2997 S3).
    """
    parts = _path_parts(raw_path)
    if not parts:
        return False
    return (
        ".claude" in parts
        or ".equipa" in parts
        or parts[-1] in _AGENT_INSTRUCTION_BASENAMES
    )


def decide_merge_gate(
    changed_files: list[str],
    *,
    security_review_blocks_merge,
    project_dir: str,
    task_id: int,
    security_review_enabled: bool = True,
    block_on_missing: bool = True,
) -> GateDecision:
    """Compute the single :class:`GateDecision` from ground truth.

    This is the ONE place the gate policy is decided. It takes the real
    branch diff (``changed_files``, produced by
    :func:`get_changed_files_for_branch`) plus a callable that reads the
    on-disk security-review artifact, and returns an immutable decision.

    NO per-task caller-supplied security-state booleans participate — those
    (the old tri-state ``review_blocks_merge`` / ``expect_artifact`` /
    ``doc_only`` signals) were the caller-trust hole task #2706 closes:

      * doc-only-ness is re-derived here via :func:`is_doc_only_diff` on the
        real file list — a caller can no longer assert ``doc_only`` to
        disable the gate.
      * when the diff is NOT doc-only, the artifact is (re-)read through the
        injected ``security_review_blocks_merge`` callable, which fails
        closed on a missing/unparseable artifact when ``block_on_missing``.

    The two GLOBAL operator-policy flags below are NOT per-task trust signals
    (they are the operator's feature-flag configuration, the same trust
    boundary as today) and are threaded through so unification does not
    silently change behaviour:

      * ``security_review_enabled`` — when the operator has disabled security
        review entirely, no artifact is ever produced, so none is expected
        (``expect_artifact=False``) and the gate does not block. This is the
        operator's explicit opt-out, not a gate weakness.
      * ``block_on_missing`` — the ``security_review_block_on_missing_
        artifact`` fail-open escape hatch (defaults fail-closed).

    LAYERING NOTE (task #2706, resolves the cycle-1 tester's open question):
    ``block_on_missing=False`` relaxes ONLY this policy layer — for a code diff
    with a missing artifact it makes this function return
    ``blocks_merge=False``. It does NOT relax the independent defensive
    invariant in ``dispatch._merge_task_branch``: because a code diff yields
    ``expect_artifact=True``, that invariant re-reads the artifact, finds it
    missing, and raises ``SecurityGateBypassError`` regardless of the flag. So
    for a code diff the fail-open flag is effectively INERT (the unified gate
    stays strictly fail-closed on a missing artifact, satisfying the #2706
    "never looser than today" constraint); the flag is only observable on the
    ``expect_artifact=False`` paths (doc-only / review-disabled), where no
    artifact is expected in the first place. This is intentional — documented,
    not a code change — so an operator never mistakes it for a way to merge an
    unreviewed code diff. See
    ``test_block_on_missing_false_is_still_caught_by_defensive_invariant``.

    ``security_review_blocks_merge`` is injected (rather than imported) so
    this leaf module stays free of a circular import on ``dispatch`` while
    the decision logic remains unit-testable in isolation. It must have the
    signature ``(project_dir, task_id, *, block_on_missing) ->
    tuple[bool, dict | None]`` — i.e. ``dispatch._security_review_blocks_merge``.

    ROLE OVERLAYS (SR-2994 S1): a diff that touches a project role overlay
    (``.equipa/roles/``) ALWAYS blocks, checked before every other rule —
    ``.md`` overlays would otherwise pass as doc-only, and an overlay is an
    agent instruction set that only the operator may approve.
    """
    overlay_files = role_overlay_changes(changed_files)
    if overlay_files:
        return GateDecision(
            blocks_merge=True,
            doc_only=False,
            expect_artifact=True,
            counts=None,
            reason="role-overlay-changed",
            changed_files=list(changed_files),
        )
    if agent_config_changes(changed_files):
        return GateDecision(
            blocks_merge=True,
            doc_only=False,
            expect_artifact=True,
            counts=None,
            reason="agent-config-changed",
            changed_files=list(changed_files),
        )
    if not security_review_enabled:
        return GateDecision(
            blocks_merge=False,
            doc_only=False,
            expect_artifact=False,
            counts=None,
            reason="security-review-disabled",
            changed_files=list(changed_files),
        )
    doc_only = is_doc_only_diff(changed_files)
    if doc_only:
        return GateDecision(
            blocks_merge=False,
            doc_only=True,
            expect_artifact=False,
            counts=None,
            reason="doc-only-diff",
            changed_files=list(changed_files),
        )
    blocks, counts = security_review_blocks_merge(
        project_dir, task_id, block_on_missing=block_on_missing,
    )
    reason = "security-review-blocked" if blocks else "clean"
    return GateDecision(
        blocks_merge=blocks,
        doc_only=False,
        expect_artifact=True,
        counts=counts,
        reason=reason,
        changed_files=list(changed_files),
    )


def _gate_audit_log(
    message: str,
    *,
    task_id: int | None = None,
    event: str | None = None,
    counts: dict | None = None,
) -> None:
    """Emit a ``[GATE-AUDIT]`` line and persist a durable audit row.

    Task #2451 Phase G — stderr behaviour (UNCHANGED by task #2702):
      * Lives in the leaf ``security_gate`` module so both ``dispatch`` and
        ``cli`` can import it without a circular dependency (GATE-07).
      * Writes ONLY to ``sys.stderr`` — the prior implementation also called
        ``logger.info`` which double-emitted in CI capture (GATE-06).
      * The ``EQUIPA_GATE_AUDIT_LOG`` env gate silences the stderr line
        (default ``"1"`` = emit) so CI capture does not double-emit.

    Task #2702 — durable persistence (NEW, additive):
      * In addition to the stderr line, each gate event is persisted to the
        ``agent_actions`` table so a post-hoc audit of "why did this branch
        merge or block" survives a lost nohup log.
      * Callers SUPPLY the structured ``task_id`` (and optional ``event`` /
        ``counts``) rather than this function parsing its own message string.
        As a compatibility net for callers that pre-date the kwargs, a narrow
        ``task=<n>`` fallback is parsed from ``message`` — the structured
        param always takes precedence.
      * Persistence is INDEPENDENT of ``EQUIPA_GATE_AUDIT_LOG``: that env gate
        only silences the stderr line (a CI double-emit concern); the audit
        trail must remain durable even when stderr is muted.
      * The DB write is best-effort FAIL-OPEN — any error is swallowed inside
        :func:`equipa.db.log_gate_audit` and can never alter the gate
        decision or raise into the merge path. ``equipa.db`` is imported
        lazily here so the leaf ``security_gate`` module keeps a clean import
        graph and an import failure cannot break the stderr path.

    Args:
        message: the human-readable audit line (emitted to stderr and stored
            verbatim as the durable record).
        task_id: task the event belongs to; enables the DB row. When ``None``
            a ``task=<n>`` token in ``message`` is used as a fallback.
        event: short event tag (e.g. ``"merge-succeeded"``) for queryable
            filtering in the persisted row.
        counts: finding counts dict, passed through to the persistence layer.
    """
    if os.environ.get("EQUIPA_GATE_AUDIT_LOG", "1") != "0":
        print(f"[GATE-AUDIT] {message}", file=sys.stderr, flush=True)

    resolved_task_id = task_id if task_id is not None else _parse_task_id(message)
    if resolved_task_id is None:
        return
    try:
        from equipa.db import log_gate_audit

        log_gate_audit(
            message, resolved_task_id, event=event, counts=counts,
        )
    except Exception:
        # Defence in depth: log_gate_audit is already fail-open, but even the
        # lazy import must never raise into the gate/merge path (task #2702).
        logger.exception("[GATE-AUDIT] audit persistence dispatch failed")


# Matches the ``task=<n>`` token that every _gate_audit_log message embeds,
# used only as a fallback when a caller does not pass the structured task_id.
_TASK_ID_RE = re.compile(r"\btask[=# ](\d+)\b")


def _parse_task_id(message: str) -> int | None:
    """Best-effort extraction of the task id from an audit ``message``.

    Fallback only — callers should pass ``task_id`` explicitly. Returns
    ``None`` when no ``task=<n>`` token is present so persistence is skipped
    rather than attributing the event to the wrong task.
    """
    match = _TASK_ID_RE.search(message or "")
    return int(match.group(1)) if match else None


def format_counts(counts: dict | None) -> str:
    """Render finding counts as ``C=N H=N M=N L=N I=N`` for audit lines.

    Defensive against ``None`` and missing keys — used at every audit site
    so future refactors cannot accidentally leak ``counts!r`` repr internals
    (GATE-09).
    """
    if counts is None:
        return "C=0 H=0 M=0 L=0 I=0"
    return (
        f"C={counts.get('CRITICAL', 0)} "
        f"H={counts.get('HIGH', 0)} "
        f"M={counts.get('MEDIUM', 0)} "
        f"L={counts.get('LOW', 0)} "
        f"I={counts.get('INFO', 0)}"
    )

# Extensions that carry executable code or executable configuration.
# A diff containing ANY file with one of these extensions cannot be
# considered "doc-only" — the security gate must run normally.
#
# Conservative by design: only well-known doc extensions skip the gate.
# An unfamiliar extension (Makefile, .toml, .yaml, .json, .lock, etc.)
# is treated as code so we never silently skip a gate the operator
# expected to run.
_DOC_EXTENSIONS: frozenset[str] = frozenset({
    ".md",
    ".markdown",
    ".rst",
    ".txt",
    ".adoc",
    ".asciidoc",
})


def is_doc_only_diff(changed_files: list[str]) -> bool:
    """Return True iff every path in ``changed_files`` is a documentation file.

    An empty list returns False on purpose: callers fetch the file list
    from ``git diff --name-only`` and an empty result usually means the
    diff couldn't be computed (worktree missing, base branch wrong, etc).
    Treating "no files reported" as "doc-only" would silently disable
    the gate on every such failure — exactly the silent-skip class of
    bug that task #2321 originally fixed.

    Agent instruction files (``.claude/**``, ``.equipa/**``, ``CLAUDE.md``,
    ``AGENTS.md``) are never doc-only even though they are ``.md``: they
    steer later agents, so skipping review on them is an injection path
    (SR-2997 S3).
    """
    if not changed_files:
        return False
    for path in changed_files:
        if is_agent_instruction_path(path):
            return False
        suffix = Path(path).suffix.lower()
        if suffix not in _DOC_EXTENSIONS:
            return False
    return True


class SecurityGateBypassError(RuntimeError):
    """Raised when ``_merge_task_branch`` is entered while the
    security-review artifact for the task reports a HIGH or CRITICAL
    finding. The defensive invariant (task #2451) is that no code path
    can ever ``git merge`` past a known-blocking review, even if a
    caller forgets to consult the gate first.
    """


async def get_changed_files_for_branch(
    project_dir: str,
    base_ref: str | None = None,
    head_ref: str = "HEAD",
) -> list[str]:
    """Return file paths changed on ``head_ref`` vs ``base_ref``.

    Uses ``git diff --name-only base_ref...head_ref`` (three-dot syntax) so
    the comparison is against the merge base, not the literal tip of
    ``base_ref`` — this matches what the eventual ``git merge`` will
    actually examine.

    ``head_ref`` defaults to ``HEAD`` (the caller's checked-out branch), but
    the unified merge gate (task #2706) passes the explicit
    ``forge-task-<id>`` branch name so it computes the SAME diff whether it
    runs in single-task mode (main checkout sits on the task branch) or
    parallel mode (main checkout sits on the default branch while the work
    lives on a shared ``forge-task-<id>`` ref). Because git worktrees share
    one object/ref store, the branch ref is visible from ``project_dir`` in
    both modes.

    When ``base_ref`` is ``None`` (the default), the repository's default
    branch is auto-detected via :func:`equipa.git_ops.get_default_branch`
    so this helper works on both ``master``- and ``main``-defaulted repos
    (task #2479).

    On any failure (timeout, missing git, ref unknown), returns an empty
    list. Callers MUST treat an empty list as "could not determine
    doc-only-ness" — :func:`is_doc_only_diff` already returns False for
    an empty list precisely so a failed lookup never silently disables
    the gate.
    """
    if base_ref is None:
        # SR-2997 S1 sibling: the diff base is the operator-named branch,
        # never origin/HEAD — an agent that repoints origin/HEAD at its own
        # branch would otherwise make the diff empty or arbitrary.
        from equipa.git_ops import (
            UntrustedDefaultBranchError,
            get_trusted_default_branch,
        )
        try:
            base_ref = get_trusted_default_branch(project_dir)
        except UntrustedDefaultBranchError as exc:
            logger.warning("[security-gate] no trusted diff base: %s", exc)
            return []
    try:
        # --no-renames: a rename OUT of a gated path (e.g. .equipa/roles/)
        # must list the source path too, not only the destination (SR-2997
        # S4). -z: paths verbatim, never C-quoted (SR-2997 S6).
        result = await git_run_async(
            ["diff", "--name-only", "--no-renames", "-z",
             f"{base_ref}...{head_ref}"],
            project_dir,
            timeout=10,
        )
    except (TimeoutError, FileNotFoundError, OSError) as exc:
        logger.warning(
            "[security-gate] could not compute changed files vs %s: %s",
            base_ref, exc,
        )
        return []
    if result.returncode != 0:
        logger.warning(
            "[security-gate] git diff vs %s exited %d: %s",
            base_ref, result.returncode, (result.stderr or "")[:200],
        )
        return []
    return [path for path in (result.stdout or "").split("\0") if path.strip()]
