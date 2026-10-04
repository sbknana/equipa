"""Security-gate helpers — task #2360 + task #2451.

This module owns the small, pure decisions the parallel and single-task
dispatch paths need to make about the security-review gate:

  * ``is_doc_only_diff(changed_files)`` — True when every changed file is
    prose: .md/.rst/.txt under docs/, or a root README*/CHANGELOG*/LICENSE*
    (gate-04; see :func:`is_doc_path`). Such diffs cannot introduce
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
import stat
import sys
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from equipa.git_ops import git_run_async, git_toplevel_async

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

# gate-07: the reviewer writes this line LAST, only once the review is done.
# A review whose final line is not the sentinel carrying this run's nonce was
# cut off (max turns, timeout, a turn-2 draft) and blocks as incomplete.
_REVIEW_COMPLETE_LINE_TEMPLATE = "<!-- EQUIPA-REVIEW-COMPLETE {nonce} -->"
_REVIEW_COMPLETE_RE = re.compile(
    r"[ \t]*<!--[ \t]*EQUIPA-REVIEW-COMPLETE[ \t]+([0-9a-f]{32})[ \t]*-->[ \t]*",
)
REVIEW_COMPLETION_SENTINEL_MISSING_REASON = "review-completion-sentinel-missing"

# gate-09 / gate-14: every line break Python's str.splitlines() honours, so
# the regex ``^``/``$`` anchors and splitlines() see the same lines. A CR-only
# or U+2028 review otherwise hid a finding heading from the MULTILINE regexes
# while splitlines() still split it.
_LINE_BREAK_RE = re.compile("\r\n|[\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
# gate-06: invisible characters that split a severity word ("HI<U+200B>GH") so
# no regex sees it, while the operator reading the rendered file does.
# Task 3143 (I-04): every Default_Ignorable_Code_Point and every Unicode Cf
# (format) character, not a hand-picked subset. The musical beam controls
# (U+1D173-1D17A), the shorthand format controls (U+1BCA0-1BCA3) and the
# Egyptian hieroglyph format controls (U+13430-1343F) split "HI?GH" too.
# Escapes, not literal characters, so the list can be read and reviewed.
_INVISIBLE_CHARS_RE = re.compile(
    "["
    "\u00ad\u034f\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2"
    "\u115f\u1160\u17b4\u17b5\u180b-\u180f"
    "\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164"
    "\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufffb"
    "\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3"
    "\U0001d173-\U0001d17a\U000e0000-\U000e0fff"
    "]"
)


# Task 3149 (R3143-07): the bidi embeddings, overrides and isolates (LRE, RLE,
# PDF, LRO, RLO, LRI, RLI, FSI, PDI). An override reverses what the reader
# sees ("<RLO>HGIH<PDF>" shows as HIGH) while every parser reads the logical
# order with the controls stripped, so a review holding one is rejected
# outright instead of read. A review names such a character ("U+202E").
_BIDI_CONTROL_RE = re.compile(
    "[\N{LEFT-TO-RIGHT EMBEDDING}-\N{RIGHT-TO-LEFT OVERRIDE}"
    "\N{LEFT-TO-RIGHT ISOLATE}-\N{POP DIRECTIONAL ISOLATE}]"
)
# A numeric character reference to one of them ("&#x202E;", "&#8238", with
# or without ";" and leading zeros) decodes to the control when the review
# is rendered, so it is rejected the same way. HTML5 names none of them.
_BIDI_CONTROL_REFERENCE_RE = re.compile(
    r"&#(?:[xX]0*(202[A-Ea-e]|206[6-9])(?![0-9A-Fa-f])"
    r"|0*(823[4-8]|829[4-7])(?![0-9]))"
)
REVIEW_BIDI_CONTROL_REASON = "review-bidi-control"
# Every line break normalize_review_text maps to "\n", and "\n" itself.
_ANY_LINE_BREAK_RE = re.compile(
    "\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85"
    "\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}]"
)


def find_bidi_control(text: str) -> str | None:
    """``"U+202E at line 3"`` for the first bidi control in ``text``, else None.

    ``text`` is read as written (before :func:`normalize_review_text`, which
    strips these characters); every line-break form counts as one break. A
    character reference to a control is reported as
    ``"U+202E reference at line 3"``, whichever comes first.
    """
    match = _BIDI_CONTROL_RE.search(text)
    reference = (_BIDI_CONTROL_REFERENCE_RE.search(text)
                 if "&#" in text else None)
    if reference is not None and (match is None
                                  or reference.start() < match.start()):
        hexadecimal, decimal = reference.group(1), reference.group(2)
        code_point = (int(hexadecimal, 16) if hexadecimal is not None
                      else int(decimal))
        line = len(_ANY_LINE_BREAK_RE.findall(text, 0, reference.start())) + 1
        return f"U+{code_point:04X} reference at line {line}"
    if match is None:
        return None
    line = len(_ANY_LINE_BREAK_RE.findall(text, 0, match.start())) + 1
    return f"U+{ord(match.group(0)):04X} at line {line}"


def normalize_review_text(text: str) -> str:
    """Canonical form of a review artifact for the nonce checks and parser.

    Strips invisible characters, applies Unicode NFKC (fullwidth ``ＨＩＧＨ``
    and mathematical ``𝐇𝐈𝐆𝐇`` become ``HIGH``) and maps every line break to
    ``\\n``. Idempotent, so a caller that already normalised can pass the
    result through again without changing it.
    """
    text = _INVISIBLE_CHARS_RE.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    # NFKC can itself produce invisible characters (U+3164 -> U+1160).
    text = _INVISIBLE_CHARS_RE.sub("", text)
    return _LINE_BREAK_RE.sub("\n", text)


def separated_review_text(text: str, separator: str) -> str:
    """:func:`normalize_review_text`, with ``separator`` for each invisible
    character instead of nothing.

    Task 3154 (I3152-01): deleting a character can join two words. The
    Hangul fillers draw a blank and many of the other characters draw
    nothing or a box, so "Rated<U+3164>HIGH" shows two words while the
    normalised text reads "RatedHIGH". The review gate reads the review
    this way too, so a word is standalone when a deleted character separates
    it. ``separator`` must be one character that NFKC keeps and that is no
    line break, so every line keeps its number.
    """
    if (len(separator) != 1 or _ANY_LINE_BREAK_RE.match(separator)
            or unicodedata.normalize("NFKC", separator) != separator):
        raise ValueError(f"unusable separator {separator!r}")
    text = _INVISIBLE_CHARS_RE.sub(separator, text)
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE_CHARS_RE.sub(separator, text)
    return _LINE_BREAK_RE.sub("\n", text)


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


# SR41-01 (task #3063): a review is a few KiB of markdown. Anything larger is
# not a review, and reading it unbounded (e.g. a symlink to /dev/zero) would
# exhaust memory, so it is treated as unreadable.
MAX_REVIEW_ARTIFACT_BYTES = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class ArtifactSnapshot:
    """One read of a review artifact: its fingerprint and the exact bytes.

    SR41-02 (task #3063): provenance is verified and findings are counted from
    this single buffer, so the sha256 in the audit log is the sha256 of the
    bytes that were parsed — a file swapped between two reads cannot be
    verified on one version and counted on another.
    """

    fingerprint: ArtifactFingerprint
    data: bytes | None = field(default=None, repr=False, compare=False)

    @property
    def text(self) -> str | None:
        """The bytes decoded as UTF-8 (lossy), or None when unreadable.

        Deliberately NOT normalised: the stable-path copy is written from
        this text and must keep the reviewer's exact bytes, or its sha256
        would no longer match the reviewer run's post-fingerprint.
        :func:`verify_reviewer_provenance` normalises for the checks.
        """
        if self.data is None:
            return None
        return self.data.decode("utf-8", errors="replace")


def _read_regular_file(path: str) -> tuple[bytes, float] | None:
    """Read ``path`` only if it is a small regular file; never block.

    SR41-01 (task #3063): the developer agent controls the worktree before
    the reviewer runs and could plant a FIFO, device or symlink at the
    artifact path. ``Path.read_bytes`` on a FIFO blocks the event loop thread
    forever, freezing every task in parallel mode. So: ``lstat`` first and
    refuse anything that is not a regular file (symlinks included), then open
    with ``O_NOFOLLOW | O_NONBLOCK`` and re-check the opened descriptor, which
    closes the swap-after-lstat race. Reads are capped at
    :data:`MAX_REVIEW_ARTIFACT_BYTES`. Returns ``(data, mtime)`` or None.
    """
    try:
        link_stat = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("[security-gate] cannot stat artifact %s: %s", path, exc)
        return None
    if not stat.S_ISREG(link_stat.st_mode):
        logger.warning(
            "[security-gate] refusing artifact %s: not a regular file "
            "(mode=%o) — treated as missing",
            path, link_stat.st_mode,
        )
        return None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        logger.warning("[security-gate] cannot open artifact %s: %s", path, exc)
        return None
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            logger.warning(
                "[security-gate] refusing artifact %s: replaced by a "
                "non-regular file while opening — treated as missing",
                path,
            )
            return None
        chunks: list[bytes] = []
        remaining = MAX_REVIEW_ARTIFACT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        logger.warning("[security-gate] cannot read artifact %s: %s", path, exc)
        return None
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > MAX_REVIEW_ARTIFACT_BYTES:
        logger.warning(
            "[security-gate] refusing artifact %s: larger than %d bytes — "
            "treated as missing",
            path, MAX_REVIEW_ARTIFACT_BYTES,
        )
        return None
    return data, opened_stat.st_mtime


def snapshot_artifact(path: str | os.PathLike) -> ArtifactSnapshot:
    """Read ``path`` once; fingerprint and bytes come from the same read."""
    artifact = os.fspath(path)
    result = _read_regular_file(artifact)
    if result is None:
        # Missing, special, oversized or unreadable: not a review. Report it
        # as absent so every caller fails closed.
        return ArtifactSnapshot(ArtifactFingerprint(path=artifact, exists=False))
    data, mtime = result
    return ArtifactSnapshot(
        ArtifactFingerprint(
            path=artifact,
            exists=True,
            sha256=hashlib.sha256(data).hexdigest(),
            mtime=mtime,
            size=len(data),
        ),
        data=data,
    )


def fingerprint_artifact(path: str | os.PathLike) -> ArtifactFingerprint:
    """Hash ``path`` and capture its mtime; ``exists=False`` when unreadable."""
    return snapshot_artifact(path).fingerprint


def read_artifact_text(path: str | os.PathLike) -> str | None:
    """The artifact decoded as UTF-8, or None when missing / not safe to read."""
    return snapshot_artifact(path).text


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
    # SR41-04 (task #3063): the nonce proves a reviewer process wrote the
    # file, not that the review was independent. These name WHICH reviewer
    # (run id, model, exact prompt) so an audit can check that afterwards.
    run_id: str = ""
    model: str | None = None
    prompt_sha256: str | None = None
    # gate-01 (task #3111): the commit the reviewer read. ``reviewed_sha`` is
    # HEAD of the reviewed worktree when the reviewer started and
    # ``reviewed_sha_end`` when it finished; ``reviewed_tree_clean`` is False
    # when tracked files differed from that commit (or the repo had a git
    # hazard), so the review did not cover exactly ``reviewed_sha``. The merge
    # uses ``reviewed_sha`` itself, never the branch name.
    reviewed_sha: str | None = None
    reviewed_sha_end: str | None = None
    reviewed_branch: str | None = None
    reviewed_tree_clean: bool = False
    reviewed_tree_detail: str = ""

    def describe_identity(self) -> str:
        """Render as ``reviewer_run=<id> model=<m> prompt_sha256=<16 hex>``."""
        return (
            f"reviewer_run={self.run_id or '-'} model={self.model or '-'} "
            f"prompt_sha256={(self.prompt_sha256 or '-')[:16]}"
        )


# SR41-03 (task #3063): the verdict when the gate finds no reviewer record
# for the task in this process. It blocks — trusting the file on disk is the
# #3035 failure mode — and is audited as its own event.
REVIEWER_RECORD_MISSING_REASON = "reviewer-record-missing"


@dataclass(frozen=True)
class ProvenanceVerdict:
    """Whether the gate may believe the artifact at ``fingerprint.path``.

    ``text`` is the decoded content of the exact bytes ``fingerprint`` hashed
    (SR41-02): callers count findings from it rather than re-reading the
    path, so the verified bytes and the parsed bytes are the same bytes.

    Task 3164 (R3161-01): ``text`` is the review AS WRITTEN, never its
    normalised form. The finding parser normalises it itself, and some of its
    views (the separated reading of task 3154, the fold-before-NFKC copy of
    task 3143) are built from the characters normalisation deletes or folds,
    so a normalised ``text`` would hide "Rated<U+3164>HIGH" from all of them.
    """

    trusted: bool
    reason: str
    fingerprint: ArtifactFingerprint
    text: str | None = field(default=None, repr=False, compare=False)
    record: ReviewerRunRecord | None = field(
        default=None, repr=False, compare=False,
    )

    @property
    def audit_event(self) -> str:
        """GATE-AUDIT event name for an untrusted verdict."""
        if self.reason == REVIEWER_RECORD_MISSING_REASON:
            return "reviewer-record-missing"
        if self.reason in _REVIEWER_FAILED_REASONS:
            return "reviewer-failed"
        if self.reason == REVIEW_COMPLETION_SENTINEL_MISSING_REASON:
            return "review-incomplete"
        return "artifact-provenance-rejected"

    def describe_reviewer(self) -> str:
        """Identity of the reviewer run this verdict rests on (SR41-04)."""
        if self.record is None:
            return "reviewer_run=none"
        return self.record.describe_identity()


_REVIEWER_RUNS: dict[str, ReviewerRunRecord] = {}
_REVIEWER_RUNS_LOCK = threading.Lock()

# SR41-03 (task #3063): hermetic callers that exercise count parsing without
# running a reviewer (the legacy gate tests) must opt in to the pre-#3041
# artifact-only trust explicitly. Production code never sets this.
_UNRECORDED_RUNS_PERMITTED = False


def set_unrecorded_reviewer_runs_permitted(permitted: bool) -> bool:
    """Allow (True) or forbid (False) gating with no reviewer record.

    For hermetic tests only. Returns the previous setting so a caller can
    restore it. Every trusted-without-record verdict is still logged with
    reason ``no-reviewer-run-recorded``.
    """
    global _UNRECORDED_RUNS_PERMITTED
    with _REVIEWER_RUNS_LOCK:
        previous = _UNRECORDED_RUNS_PERMITTED
        _UNRECORDED_RUNS_PERMITTED = bool(permitted)
    return previous


def unrecorded_reviewer_runs_permitted() -> bool:
    """Whether a gate evaluation with no reviewer record may trust the file."""
    with _REVIEWER_RUNS_LOCK:
        return _UNRECORDED_RUNS_PERMITTED


def new_reviewer_nonce() -> str:
    """Mint a fresh 128-bit reviewer-run nonce (32 lowercase hex chars)."""
    return secrets.token_hex(16)


def new_reviewer_run_id() -> str:
    """Mint an identifier for one reviewer run (16 lowercase hex chars)."""
    return secrets.token_hex(8)


def reviewer_prompt_sha256(prompt: str) -> str:
    """sha256 of the exact prompt a reviewer attempt was started with."""
    return hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()


def reviewer_nonce_line(nonce: str) -> str:
    """The exact line a reviewer run must write into its artifact."""
    return _REVIEWER_NONCE_LINE_TEMPLATE.format(nonce=nonce)


def artifact_nonces(text: str) -> set[str]:
    """Every reviewer-run nonce line present in ``text``."""
    return set(_REVIEWER_NONCE_RE.findall(normalize_review_text(text or "")))


def review_complete_line(nonce: str) -> str:
    """The completion sentinel a reviewer run must write as its LAST line."""
    return _REVIEW_COMPLETE_LINE_TEMPLATE.format(nonce=nonce)


def review_completion_nonce(text: str | None) -> str | None:
    """The nonce of the completion sentinel ending ``text``, else None.

    Only the last non-blank line counts (gate-07): a sentinel followed by
    anything else means the review kept going after it was declared done,
    and a sentinel quoted mid-file proves nothing.
    """
    lines = normalize_review_text(text or "").rstrip().rsplit("\n", 1)
    match = _REVIEW_COMPLETE_RE.fullmatch(lines[-1])
    return match.group(1) if match is not None else None


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
      * ``review-completion-sentinel-missing`` — its last line is not this
        run's ``<!-- EQUIPA-REVIEW-COMPLETE <nonce> -->`` sentinel, so the
        review was never finished (gate-07);
      * ``artifact-changed-after-review`` — edited after the reviewer ended.
      * ``review-bidi-control-U+XXXX-at-line-N`` — the bytes hold a bidi
        embedding, override or isolate (task 3149, R3143-07), checked first.

      * ``reviewer-record-missing`` — no reviewer run was recorded for the
        task in this process (SR41-03, task #3063). Both production gate
        paths run (or explicitly skip as doc-only) the reviewer in-process
        first, so a missing record means the gate was reached some other way
        — a resume, a re-gate, a merge after a restart — and the file on disk
        is exactly what #3035 wrongly trusted. Only hermetic callers that
        opted in via :func:`set_unrecorded_reviewer_runs_permitted` get the
        pre-#3041 artifact-only trust (reason ``no-reviewer-run-recorded``).

    The artifact is read ONCE (SR41-02): the fingerprint, the nonce check and
    the returned ``text`` all come from the same bytes. The nonce check and
    the completion sentinel read the text normalised with
    :func:`normalize_review_text` (gate-09 / gate-14), so they see the same
    ``\\n``-separated lines whatever line breaks or invisible characters the
    file used. The returned ``text`` is the text as written (task 3164,
    R3161-01): the finding parser normalises it itself, after reading the
    characters normalisation would delete.
    """
    snapshot = snapshot_artifact(review_path)
    fingerprint = snapshot.fingerprint
    raw_text = snapshot.text
    text = normalize_review_text(raw_text) if raw_text is not None else None
    record = get_reviewer_run(task_id)

    def verdict(trusted: bool, reason: str) -> ProvenanceVerdict:
        return ProvenanceVerdict(
            trusted, reason, fingerprint, text=raw_text, record=record,
        )

    # Task 3149 (R3143-07): the normalised text has lost its bidi controls,
    # so they are looked for in the bytes as written, before anything else.
    bidi = find_bidi_control(raw_text) if raw_text is not None else None
    if bidi is not None:
        return verdict(False, f"{REVIEW_BIDI_CONTROL_REASON}-"
                              + bidi.replace(" ", "-"))
    if record is None:
        if unrecorded_reviewer_runs_permitted():
            return verdict(True, "no-reviewer-run-recorded")
        return verdict(False, REVIEWER_RECORD_MISSING_REASON)
    if record.status != REVIEWER_STATUS_SUCCEEDED:
        return verdict(False, f"reviewer-{record.status}")
    if not fingerprint.exists or text is None:
        return verdict(False, "artifact-missing")
    post = record.post_artifact
    if post is None or not post.exists:
        return verdict(False, "artifact-not-written-by-reviewer")
    pre = record.pre_artifact
    if pre is not None and pre.exists and pre.sha256 == fingerprint.sha256:
        return verdict(False, "artifact-pre-existing")
    if record.nonce not in artifact_nonces(text):
        return verdict(False, "reviewer-nonce-missing")
    if post.sha256 != fingerprint.sha256:
        return verdict(False, "artifact-changed-after-review")
    if review_completion_nonce(text) != record.nonce:
        return verdict(False, REVIEW_COMPLETION_SENTINEL_MISSING_REASON)
    return verdict(True, "verified")


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
        f"{record.describe_identity()} "
        f"reviewed_sha={(record.reviewed_sha or 'none')[:12]} "
        f"reviewed_sha_end={(record.reviewed_sha_end or 'none')[:12]} "
        f"tree_clean={record.reviewed_tree_clean} "
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
_AGENT_INSTRUCTION_BASENAMES: frozenset[str] = frozenset({
    "claude.md", "agents.md", "gemini.md",
})
# gate-04: repository-root directories whose files EQUIPA loads as role
# prompts, skills and standing orders (``prompts/`` and ``skills/`` are the
# trees skill_manifest.json integrity-protects). A change there rewrites the
# instructions of later agents, the security reviewer included.
_AGENT_INSTRUCTION_ROOT_DIRS: frozenset[str] = frozenset({
    "prompts", "standing_orders", "skills",
})
# ``*SKILL.md`` at any depth: the skill file convention of every agent CLI.
_SKILL_FILE_SUFFIX = "skill.md"


def _is_agent_config_path(raw_path: str) -> bool:
    """True for a path whose change only the operator may merge."""
    parts = _path_parts(raw_path)
    if not parts:
        return False
    return (
        ".claude" in parts
        or parts[0] in _AGENT_INSTRUCTION_ROOT_DIRS
        or parts[-1].endswith(_SKILL_FILE_SUFFIX)
    )


def agent_config_changes(changed_files: list[str]) -> list[str]:
    """Return the changed paths that configure or instruct later agents.

    SR-2997 S3: ``.claude/skills|agents|commands/*.md`` and
    ``.claude/settings*.json`` (at any depth) are loaded by the Claude CLI
    for every later agent (gate roles included) that runs with the worktree
    as ``--add-dir``. gate-04 adds EQUIPA's own instruction paths: anything
    under a root ``prompts/``, ``standing_orders/`` or ``skills/`` directory
    and any ``*SKILL.md``. They are agent instructions (and hooks), so only
    the operator may merge them — the gate fails closed on them exactly like
    ``.equipa/roles/``.
    """
    return [path for path in changed_files if _is_agent_config_path(path)]


def is_agent_instruction_path(raw_path: str) -> bool:
    """True for paths an agent CLI reads as instructions, never plain docs.

    Covers every agent-config path (:func:`agent_config_changes`), any
    ``.equipa`` component, a root ``.github`` directory (Copilot instructions
    and CI workflows) and ``CLAUDE.md`` / ``AGENTS.md`` / ``GEMINI.md`` at any
    depth. Such a file must never be auto-merged as "doc-only" without a
    security review (SR-2997 S3, gate-04).
    """
    parts = _path_parts(raw_path)
    if not parts:
        return False
    return (
        _is_agent_config_path(raw_path)
        or ".equipa" in parts
        or parts[0] == ".github"
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
    try:
        blocks, counts = security_review_blocks_merge(
            project_dir, task_id, block_on_missing=block_on_missing,
        )
    except Exception as error:  # noqa: BLE001 - any failure blocks the merge
        # Task 3149 (R3143-04): an exception while reading or parsing the
        # review is a logged block with a verdict, never an exception that
        # ends the task with no GATE-AUDIT line.
        logger.exception("[security-gate] review check for task %s failed",
                         task_id)
        reason = f"review parse error: {type(error).__name__}"
        _gate_audit_log(
            f"task={task_id} event=review-parse-error reason={reason!r} "
            f"action=block",
            task_id=task_id,
            event="review-parse-error",
        )
        return GateDecision(
            blocks_merge=True,
            doc_only=False,
            expect_artifact=True,
            counts=None,
            reason=reason,
            changed_files=list(changed_files),
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


def escape_audit_text(text: str) -> str:
    """``text`` with every line-breaking or control character escaped.

    Audit messages embed branch-authored text (file names, git output). A raw
    CR, LF, other C0/C1 control or a Unicode line/paragraph separator in it
    could start what reads as a second, forged ``[GATE-AUDIT]`` line, or
    rewrite the current one on a terminal (N-01, task #3146). Each such
    character becomes a visible ``\\xNN`` / ``\\uNNNN`` escape, so the line
    stays one line whatever the embedded text holds.
    """
    if text.isprintable():
        return text
    escaped: list[str] = []
    for char in text:
        code = ord(char)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            escaped.append(f"\\x{code:02x}")
        elif char in "\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}":
            escaped.append(f"\\u{code:04x}")
        else:
            escaped.append(char)
    return "".join(escaped)


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
        print(
            f"[GATE-AUDIT] {escape_audit_text(message)}",
            file=sys.stderr, flush=True,
        )

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

# gate-04: "doc-only" is an ALLOWLIST of locations, not of extensions. By
# extension alone, ``.txt`` / ``.md`` covered dependency manifests
# (requirements.txt), build scripts (CMakeLists.txt) and agent prompts, and
# all of them merged with no review. Prose means one of these extensions
# under the root ``docs/`` directory, or a root README* / CHANGELOG* /
# LICENSE* file. Everything else is reviewed.
_DOC_PROSE_EXTENSIONS: frozenset[str] = frozenset({".md", ".rst", ".txt"})
_ROOT_DOC_BASENAME_PREFIXES: tuple[str, ...] = ("readme", "changelog", "license")
_ROOT_DOC_EXTENSIONS: frozenset[str] = _DOC_PROSE_EXTENSIONS | {""}
_DOCS_ROOT_DIR = "docs"
# Build and dependency inputs that carry a prose extension, and git
# attribute/submodule files. Never doc-only, wherever they sit.
_NEVER_DOC_BASENAME_RE = re.compile(
    r"(?:requirements|constraints)[^/]*\.txt|cmakelists\.txt"
    r"|\.gitattributes|\.gitmodules",
)


def is_doc_path(raw_path: str) -> bool:
    """True when ``raw_path`` is prose a security review may skip (gate-04).

    Case-insensitive, like every other gate path check, so ``Docs/`` or
    ``ReadMe.md`` cannot sidestep a rule the lower-case spelling obeys.
    """
    parts = _path_parts(raw_path)
    if not parts or ".." in parts:
        return False
    if is_agent_instruction_path(raw_path):
        return False
    basename = parts[-1]
    if _NEVER_DOC_BASENAME_RE.fullmatch(basename):
        return False
    suffix = PurePosixPath(basename).suffix
    if len(parts) == 1:
        return (
            basename.startswith(_ROOT_DOC_BASENAME_PREFIXES)
            and suffix in _ROOT_DOC_EXTENSIONS
        )
    return parts[0] == _DOCS_ROOT_DIR and suffix in _DOC_PROSE_EXTENSIONS


def is_doc_only_diff(changed_files: list[str]) -> bool:
    """Return True iff every path in ``changed_files`` is prose (gate-04).

    Prose is a ``.md`` / ``.rst`` / ``.txt`` file under the root ``docs/``
    directory, or a root ``README*`` / ``CHANGELOG*`` / ``LICENSE*`` file
    (see :func:`is_doc_path`).

    An empty list returns False on purpose: callers fetch the file list
    from ``git diff --name-only`` and an empty result usually means the
    diff couldn't be computed (worktree missing, base branch wrong, etc).
    Treating "no files reported" as "doc-only" would silently disable
    the gate on every such failure — exactly the silent-skip class of
    bug that task #2321 originally fixed.

    Never doc-only, whatever the extension or location: agent instruction
    paths (``.claude/**``, ``.equipa/**``, ``.github/**``, root
    ``prompts/`` / ``standing_orders/`` / ``skills/``, ``*SKILL.md``,
    ``CLAUDE.md``, ``AGENTS.md``, ``GEMINI.md``), which steer later agents
    (SR-2997 S3), and ``requirements*.txt`` / ``constraints*.txt`` /
    ``CMakeLists.txt`` / ``.gitattributes`` / ``.gitmodules``, which change
    what gets installed, built or checked out.

    A submodule pointer (:class:`SubmodulePointerPath`, a gitlink in the
    diff) is never doc-only whatever its name: it pulls in code the diff
    does not show (IND-01, task #3132).
    """
    if not changed_files:
        return False
    if any(isinstance(path, SubmodulePointerPath) for path in changed_files):
        return False
    return all(is_doc_path(path) for path in changed_files)


class SubmodulePointerPath(str):
    """A changed path that is a submodule pointer (gitlink, mode 160000).

    Returned by :func:`get_changed_files_for_branch`. It is an ordinary
    ``str`` for every other purpose; :func:`is_doc_only_diff` refuses it.
    ``old_sha`` / ``new_sha`` (task #3146) are the submodule commits the
    pointer moves between (all zeros when it is added or removed).
    """

    old_sha: str
    new_sha: str

    def __new__(
        cls, path: str, old_sha: str = "", new_sha: str = "",
    ) -> SubmodulePointerPath:
        instance = super().__new__(cls, path)
        instance.old_sha = old_sha
        instance.new_sha = new_sha
        return instance


def describe_submodule_pointer_changes(changed_files: list[str]) -> str | None:
    """Reviewer note listing every submodule pointer change, or None.

    IND3132-02 (task #3146): a committed ``.gitmodules`` ``ignore = all``
    (or similar config) hides a gitlink bump from a plain ``git diff``, so
    the reviewer must be told about it explicitly. Paths are branch-authored
    and quoted; SHAs are git's hex output.
    """
    pointers = [
        path for path in changed_files if isinstance(path, SubmodulePointerPath)
    ]
    if not pointers:
        return None
    lines = [
        "ORCHESTRATOR NOTE (added by the merge gate, not part of the task): "
        "this branch changes the following submodule pointer(s). A plain "
        "`git diff` may hide them (`.gitmodules` `ignore = all` or "
        "`diff.ignoreSubmodules`); `git diff --ignore-submodules=none` shows "
        "them. The code they point at is outside this repository's diff. "
        "Review every pointer change and report one you cannot verify as a "
        "finding:",
    ]
    for path in pointers[:50]:
        lines.append(
            f"- {path[:200]!r}: {(path.old_sha or 'unknown')[:40]} -> "
            f"{(path.new_sha or 'unknown')[:40]}"
        )
    if len(pointers) > 50:
        lines.append(f"- ... and {len(pointers) - 50} more")
    return "\n".join(lines)


# git's file mode for a gitlink (a submodule pointer) in a tree.
_GITLINK_MODE = "160000"


def _parse_raw_diff_z(raw: str) -> list[str] | None:
    """Changed paths from ``git diff --raw -z``, gitlinks marked.

    Each record is ``:<old mode> <new mode> <old sha> <new sha> <status>``
    followed by one path, or two for a rename / copy. None when the output
    does not have that shape (callers then fail closed).
    """
    fields = raw.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    paths: list[str] = []
    index = 0
    while index < len(fields):
        header = fields[index].split()
        if len(header) != 5 or not header[0].startswith(":"):
            return None
        old_mode, new_mode, status = header[0][1:], header[1], header[4]
        old_sha, new_sha = header[2], header[3]
        path_count = 2 if status[:1] in ("R", "C") else 1
        record_paths = fields[index + 1:index + 1 + path_count]
        if len(record_paths) != path_count:
            return None
        gitlink = _GITLINK_MODE in (old_mode, new_mode)
        for path in record_paths:
            # IND3132-03 (task #3146): a name that is only whitespace (or
            # otherwise odd) is still a changed file; is_doc_path judges it.
            # git never prints an empty path, so one means malformed output.
            if not path:
                return None
            paths.append(
                SubmodulePointerPath(path, old_sha, new_sha) if gitlink else path
            )
        index += 1 + path_count
    return paths


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

    Uses ``git diff --raw base_ref...head_ref`` (three-dot syntax) so
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

    R3119-01 (task #3126): the diff runs at the work-tree ROOT of
    ``project_dir`` with ``--no-relative``. ``project_dir`` may be a
    project nested in a sub-directory of its repository, and from there an
    agent-written ``diff.relative=true`` hid every change outside it. Paths
    are always relative to the work-tree root.

    IND-01 / IND-02 (task #3132): ``--ignore-submodules=none`` overrides
    every config that hides a submodule pointer bump (``diff.ignoreSubmodules``,
    ``submodule.<name>.ignore`` in config or a committed ``.gitmodules``),
    and ``--raw`` modes mark each gitlink as a :class:`SubmodulePointerPath`.
    The work-tree root must contain ``project_dir``, so a ``core.worktree``
    naming another checkout yields no list (fail closed), not that
    checkout's diff.
    """
    repo_root = await git_toplevel_async(project_dir)
    if repo_root is None:
        logger.warning(
            "[security-gate] %s is not inside a readable git work tree", project_dir,
        )
        return []
    project_dir = str(repo_root)
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
            ["diff", "--raw", "--no-abbrev", "--no-renames", "--no-relative",
             "--ignore-submodules=none", "-z", f"{base_ref}...{head_ref}"],
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
    changed = _parse_raw_diff_z(result.stdout or "")
    if changed is None:
        logger.warning(
            "[security-gate] unexpected git diff --raw output vs %s", base_ref,
        )
        return []
    gitlinks = [path for path in changed if isinstance(path, SubmodulePointerPath)]
    if gitlinks:
        logger.warning(
            "[security-gate] submodule pointer change(s) %s: never doc-only, "
            "review required", ", ".join(gitlinks[:5]),
        )
    return changed
