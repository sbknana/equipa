"""EQUIPA Task Flow helpers — durable revisions + sticky cancel.

A *flow* is a multi-step orchestration that spans one or more child tasks
(for example a ``developer -> [security-reviewer, code-reviewer,
integration-tester]`` fanout). Flows are stored in TheForge alongside the
tasks they manage so they survive a Claudinator restart.

Key properties (inspired by the OpenClaw Task Flow design):

* **Durable revisions.** Every state transition bumps an integer
  ``revision`` counter and appends an immutable row to ``flow_revisions``.
  Optimistic concurrency (compare-and-swap on ``revision``) keeps two
  dispatchers from clobbering each other.

* **Sticky cancel.** Cancelling a flow:
    1. Sets ``state = 'cancelled'`` and stamps ``cancelled_at``.
    2. Marks every non-terminal child ``flow_tasks`` row as ``cancelled``.
    3. Updates each child task's ``status`` to ``cancelled`` so workers
       observe the cancellation on the next DB read.
    4. Refuses any subsequent ``add_child`` / ``transition`` call — the
       cancel is sticky, not a soft pause.

* **Managed vs mirrored.** A child task may be either ``managed`` (created
  by ``create_flow``/``add_child`` for this flow) or ``mirrored`` (a
  pre-existing task pulled into the flow for tracking only). Sticky cancel
  propagates to both, but only managed tasks are deleted on flow abort.

The module is a thin layer over ``equipa.db.db_conn`` and uses only stdlib.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from equipa.db import db_conn

logger = logging.getLogger(__name__)


# --- Constants ---

VALID_FLOW_STATES = frozenset(
    {"queued", "running", "paused", "cancelled", "done", "failed"}
)
TERMINAL_FLOW_STATES = frozenset({"cancelled", "done", "failed"})

VALID_CHILD_STATES = frozenset(
    {"pending", "running", "done", "failed", "cancelled"}
)
TERMINAL_CHILD_STATES = frozenset({"done", "failed", "cancelled"})

VALID_RELATIONSHIPS = frozenset({"managed", "mirrored"})


class FlowError(RuntimeError):
    """Base class for flow-layer errors."""


class FlowNotFound(FlowError):
    """Raised when a flow_id does not exist in TheForge."""


class FlowCancelled(FlowError):
    """Raised when an operation is attempted on a sticky-cancelled flow."""


class FlowRevisionConflict(FlowError):
    """Raised when an optimistic-concurrency CAS on revision fails."""


# --- Dataclasses ---

@dataclass(frozen=True)
class Flow:
    """Snapshot of a flow row."""

    id: int
    project_id: int
    parent_task_id: int | None
    title: str
    state: str
    revision: int
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    cancelled_at: str | None
    cancelled_reason: str | None
    completed_at: str | None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_FLOW_STATES

    @property
    def is_cancelled(self) -> bool:
        return self.state == "cancelled"


@dataclass(frozen=True)
class FlowTask:
    """Snapshot of a flow_tasks row."""

    id: int
    flow_id: int
    task_id: int
    role: str | None
    relationship: str
    state: str
    added_at: str
    completed_at: str | None


# --- Internal helpers ---

def _row_to_flow(row: sqlite3.Row) -> Flow:
    raw_meta = row["metadata"]
    meta: dict[str, Any]
    if not raw_meta:
        meta = {}
    else:
        try:
            meta = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "[flows] flow_id=%s has malformed metadata JSON; treating as empty",
                row["id"],
            )
            meta = {}
    return Flow(
        id=row["id"],
        project_id=row["project_id"],
        parent_task_id=row["parent_task_id"],
        title=row["title"],
        state=row["state"],
        revision=row["revision"],
        metadata=meta,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        cancelled_at=row["cancelled_at"],
        cancelled_reason=row["cancelled_reason"],
        completed_at=row["completed_at"],
    )


def _row_to_flow_task(row: sqlite3.Row) -> FlowTask:
    return FlowTask(
        id=row["id"],
        flow_id=row["flow_id"],
        task_id=row["task_id"],
        role=row["role"],
        relationship=row["relationship"],
        state=row["state"],
        added_at=row["added_at"],
        completed_at=row["completed_at"],
    )


def _append_revision(
    conn: sqlite3.Connection,
    flow_id: int,
    revision: int,
    state: str,
    event: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append a row to the flow_revisions audit log (caller commits)."""
    conn.execute(
        "INSERT INTO flow_revisions (flow_id, revision, state, event, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            flow_id,
            revision,
            state,
            event,
            json.dumps(payload) if payload is not None else None,
        ),
    )


def _load_flow_for_update(
    conn: sqlite3.Connection, flow_id: int
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM flows WHERE id = ?", (flow_id,)
    ).fetchone()
    if row is None:
        raise FlowNotFound(f"flow_id={flow_id} does not exist")
    return row


# --- Public API ---

def create_flow(
    project_id: int,
    title: str,
    parent_task_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> Flow:
    """Create a new flow in the ``queued`` state.

    Returns the created Flow snapshot. Writes an initial revision=0 row to
    ``flow_revisions`` so the audit log is never empty.
    """
    if not title or not title.strip():
        raise ValueError("flow title must be non-empty")
    if metadata is None:
        metadata = {}
    meta_json = json.dumps(metadata)

    with db_conn(write=True) as conn:
        cur = conn.execute(
            "INSERT INTO flows (project_id, parent_task_id, title, state, "
            "revision, metadata) VALUES (?, ?, ?, 'queued', 0, ?)",
            (project_id, parent_task_id, title, meta_json),
        )
        flow_id = cur.lastrowid
        _append_revision(
            conn,
            flow_id,
            revision=0,
            state="queued",
            event="create",
            payload={"title": title, "parent_task_id": parent_task_id},
        )
        row = _load_flow_for_update(conn, flow_id)
    return _row_to_flow(row)


def get_flow(flow_id: int) -> Flow:
    """Return a fresh Flow snapshot. Raises FlowNotFound."""
    with db_conn(write=False) as conn:
        row = conn.execute(
            "SELECT * FROM flows WHERE id = ?", (flow_id,)
        ).fetchone()
    if row is None:
        raise FlowNotFound(f"flow_id={flow_id} does not exist")
    return _row_to_flow(row)


def list_active_flows(project_id: int | None = None) -> list[Flow]:
    """Return all non-terminal flows, optionally scoped to a project."""
    sql = (
        "SELECT * FROM flows WHERE state NOT IN ('cancelled','done','failed')"
    )
    params: tuple = ()
    if project_id is not None:
        sql += " AND project_id = ?"
        params = (project_id,)
    sql += " ORDER BY created_at"
    with db_conn(write=False) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_flow(r) for r in rows]


def transition(
    flow_id: int,
    new_state: str,
    expected_revision: int | None = None,
    event: str = "transition",
    payload: dict[str, Any] | None = None,
) -> Flow:
    """Atomically transition a flow to ``new_state`` and bump its revision.

    Args:
        flow_id: target flow.
        new_state: one of VALID_FLOW_STATES.
        expected_revision: if not None, the call fails with
            FlowRevisionConflict unless the current revision matches. Use
            this for optimistic-concurrency control across dispatchers.
        event: short tag stored in flow_revisions.event.
        payload: optional JSON-serialisable dict for the audit row.

    Refuses to transition out of a terminal state. Refuses to transition a
    cancelled flow at all (sticky cancel).
    """
    if new_state not in VALID_FLOW_STATES:
        raise ValueError(f"invalid state {new_state!r}")

    with db_conn(write=True) as conn:
        row = _load_flow_for_update(conn, flow_id)
        current_state = row["state"]
        current_rev = row["revision"]

        if current_state == "cancelled":
            raise FlowCancelled(
                f"flow_id={flow_id} is sticky-cancelled; refusing transition "
                f"to {new_state!r}"
            )
        if current_state in TERMINAL_FLOW_STATES and new_state != current_state:
            raise FlowError(
                f"flow_id={flow_id} is terminal ({current_state!r}); refusing "
                f"transition to {new_state!r}"
            )
        if expected_revision is not None and current_rev != expected_revision:
            raise FlowRevisionConflict(
                f"flow_id={flow_id} expected revision {expected_revision}, "
                f"got {current_rev}"
            )

        new_rev = current_rev + 1
        completed_clause = ""
        completed_params: tuple = ()
        if new_state in {"done", "failed"}:
            completed_clause = ", completed_at = datetime('now')"

        conn.execute(
            f"UPDATE flows SET state = ?, revision = ?, "
            f"updated_at = datetime('now'){completed_clause} "
            f"WHERE id = ? AND revision = ?",
            (new_state, new_rev, flow_id, current_rev) + completed_params,
        )
        # Verify the CAS — guards against a concurrent writer slipping in
        # between our SELECT and UPDATE.
        check = conn.execute(
            "SELECT revision FROM flows WHERE id = ?", (flow_id,)
        ).fetchone()
        if check is None or check["revision"] != new_rev:
            raise FlowRevisionConflict(
                f"flow_id={flow_id} CAS update failed (concurrent writer)"
            )
        _append_revision(
            conn,
            flow_id,
            revision=new_rev,
            state=new_state,
            event=event,
            payload=payload,
        )
        row = _load_flow_for_update(conn, flow_id)
    return _row_to_flow(row)


def add_child(
    flow_id: int,
    task_id: int,
    role: str | None = None,
    relationship: str = "managed",
) -> FlowTask:
    """Attach a task to a flow.

    Refuses to attach if the flow is sticky-cancelled or otherwise terminal.
    Idempotent: re-adding the same (flow_id, task_id) returns the existing
    row instead of erroring.
    """
    if relationship not in VALID_RELATIONSHIPS:
        raise ValueError(f"invalid relationship {relationship!r}")

    with db_conn(write=True) as conn:
        row = _load_flow_for_update(conn, flow_id)
        if row["state"] == "cancelled":
            raise FlowCancelled(
                f"flow_id={flow_id} is sticky-cancelled; refusing to attach "
                f"task_id={task_id}"
            )
        if row["state"] in TERMINAL_FLOW_STATES:
            raise FlowError(
                f"flow_id={flow_id} is terminal ({row['state']!r}); refusing "
                f"to attach task_id={task_id}"
            )

        existing = conn.execute(
            "SELECT * FROM flow_tasks WHERE flow_id = ? AND task_id = ?",
            (flow_id, task_id),
        ).fetchone()
        if existing is not None:
            return _row_to_flow_task(existing)

        cur = conn.execute(
            "INSERT INTO flow_tasks (flow_id, task_id, role, relationship, "
            "state) VALUES (?, ?, ?, ?, 'pending')",
            (flow_id, task_id, role, relationship),
        )
        new_rev = row["revision"] + 1
        conn.execute(
            "UPDATE flows SET revision = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (new_rev, flow_id),
        )
        _append_revision(
            conn,
            flow_id,
            revision=new_rev,
            state=row["state"],
            event="child_added",
            payload={
                "task_id": task_id,
                "role": role,
                "relationship": relationship,
            },
        )
        new_row = conn.execute(
            "SELECT * FROM flow_tasks WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _row_to_flow_task(new_row)


def update_child_state(
    flow_id: int,
    task_id: int,
    new_state: str,
    payload: dict[str, Any] | None = None,
) -> FlowTask:
    """Update a child task's state within a flow.

    Refuses if the flow is sticky-cancelled — the cancel is authoritative
    and any incoming "I finished" signal must not flip a cancelled child
    back to ``done``.
    """
    if new_state not in VALID_CHILD_STATES:
        raise ValueError(f"invalid child state {new_state!r}")

    with db_conn(write=True) as conn:
        flow_row = _load_flow_for_update(conn, flow_id)
        if flow_row["state"] == "cancelled":
            raise FlowCancelled(
                f"flow_id={flow_id} is sticky-cancelled; refusing child "
                f"state update for task_id={task_id}"
            )

        child = conn.execute(
            "SELECT * FROM flow_tasks WHERE flow_id = ? AND task_id = ?",
            (flow_id, task_id),
        ).fetchone()
        if child is None:
            raise FlowError(
                f"task_id={task_id} is not a child of flow_id={flow_id}"
            )

        completed_clause = ""
        if new_state in TERMINAL_CHILD_STATES:
            completed_clause = ", completed_at = datetime('now')"
        conn.execute(
            f"UPDATE flow_tasks SET state = ?{completed_clause} WHERE id = ?",
            (new_state, child["id"]),
        )
        new_rev = flow_row["revision"] + 1
        conn.execute(
            "UPDATE flows SET revision = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (new_rev, flow_id),
        )
        _append_revision(
            conn,
            flow_id,
            revision=new_rev,
            state=flow_row["state"],
            event="child_state",
            payload={"task_id": task_id, "new_state": new_state, **(payload or {})},
        )
        new_row = conn.execute(
            "SELECT * FROM flow_tasks WHERE id = ?", (child["id"],)
        ).fetchone()
    return _row_to_flow_task(new_row)


def list_children(flow_id: int) -> list[FlowTask]:
    """Return every flow_tasks row attached to ``flow_id``."""
    with db_conn(write=False) as conn:
        rows = conn.execute(
            "SELECT * FROM flow_tasks WHERE flow_id = ? ORDER BY added_at, id",
            (flow_id,),
        ).fetchall()
    return [_row_to_flow_task(r) for r in rows]


def cancel_flow(
    flow_id: int,
    reason: str | None = None,
    expected_revision: int | None = None,
) -> Flow:
    """Sticky-cancel a flow.

    Atomically:
      1. Sets the flow's state to ``cancelled`` and bumps revision.
      2. Marks every non-terminal child ``flow_tasks`` row as ``cancelled``.
      3. Updates each affected child task's ``tasks.status`` to
         ``cancelled`` so worker code observes the propagation.
      4. Appends a ``cancel`` revision row.

    Re-cancelling an already-cancelled flow is a no-op (returns the current
    snapshot) — sticky cancel is idempotent.
    """
    with db_conn(write=True) as conn:
        row = _load_flow_for_update(conn, flow_id)
        if row["state"] == "cancelled":
            return _row_to_flow(row)
        if expected_revision is not None and row["revision"] != expected_revision:
            raise FlowRevisionConflict(
                f"flow_id={flow_id} expected revision {expected_revision}, "
                f"got {row['revision']}"
            )

        new_rev = row["revision"] + 1
        conn.execute(
            "UPDATE flows SET state = 'cancelled', revision = ?, "
            "cancelled_at = datetime('now'), cancelled_reason = ?, "
            "updated_at = datetime('now') WHERE id = ? AND revision = ?",
            (new_rev, reason, flow_id, row["revision"]),
        )
        # Verify CAS
        check = conn.execute(
            "SELECT revision FROM flows WHERE id = ?", (flow_id,)
        ).fetchone()
        if check is None or check["revision"] != new_rev:
            raise FlowRevisionConflict(
                f"flow_id={flow_id} CAS cancel failed (concurrent writer)"
            )

        # Cancel non-terminal children.
        children = conn.execute(
            "SELECT id, task_id FROM flow_tasks "
            "WHERE flow_id = ? AND state NOT IN ('done','failed','cancelled')",
            (flow_id,),
        ).fetchall()
        cancelled_task_ids: list[int] = []
        for c in children:
            conn.execute(
                "UPDATE flow_tasks SET state = 'cancelled', "
                "completed_at = datetime('now') WHERE id = ?",
                (c["id"],),
            )
            cancelled_task_ids.append(c["task_id"])

        # Propagate to underlying tasks: any task that is not already in a
        # terminal status flips to 'cancelled' so a worker reading the row
        # observes the cancellation.
        if cancelled_task_ids:
            placeholders = ",".join("?" * len(cancelled_task_ids))
            conn.execute(
                f"UPDATE tasks SET status = 'cancelled' "
                f"WHERE id IN ({placeholders}) "
                f"AND status NOT IN ('done','completed','failed','cancelled')",
                cancelled_task_ids,
            )

        _append_revision(
            conn,
            flow_id,
            revision=new_rev,
            state="cancelled",
            event="cancel",
            payload={
                "reason": reason,
                "cancelled_task_ids": cancelled_task_ids,
            },
        )
        row = _load_flow_for_update(conn, flow_id)
    return _row_to_flow(row)


def is_cancelled(flow_id: int) -> bool:
    """Cheap check — workers poll this to honour sticky cancel mid-task."""
    with db_conn(write=False) as conn:
        row = conn.execute(
            "SELECT state FROM flows WHERE id = ?", (flow_id,)
        ).fetchone()
    if row is None:
        raise FlowNotFound(f"flow_id={flow_id} does not exist")
    return row["state"] == "cancelled"


def get_revisions(flow_id: int, limit: int | None = None) -> list[dict[str, Any]]:
    """Return the audit log for ``flow_id`` ordered by revision ascending."""
    sql = (
        "SELECT id, flow_id, revision, state, event, payload, created_at "
        "FROM flow_revisions WHERE flow_id = ? ORDER BY revision ASC"
    )
    params: tuple = (flow_id,)
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params = (flow_id, limit)
    with db_conn(write=False) as conn:
        rows = conn.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        payload: dict[str, Any] | None
        if not r["payload"]:
            payload = None
        else:
            try:
                payload = json.loads(r["payload"])
            except (json.JSONDecodeError, TypeError):
                payload = None
        out.append(
            {
                "id": r["id"],
                "flow_id": r["flow_id"],
                "revision": r["revision"],
                "state": r["state"],
                "event": r["event"],
                "payload": payload,
                "created_at": r["created_at"],
            }
        )
    return out


def reconcile_after_restart(flow_id: int) -> Flow:
    """Recovery hook used after a Claudinator restart.

    Inspects each child task's underlying ``tasks.status``:
      * if every child is in a terminal state, the flow transitions to
        ``done`` (or ``failed`` if any child failed).
      * if any child is still running, the flow is left in ``running``.
      * if the flow is sticky-cancelled, this is a no-op (returns current).

    Returns the post-reconcile Flow snapshot.
    """
    flow = get_flow(flow_id)
    if flow.is_cancelled:
        return flow

    children = list_children(flow_id)
    if not children:
        return flow

    states = {c.state for c in children}
    if states <= TERMINAL_CHILD_STATES:
        next_state = "failed" if "failed" in states else "done"
        if flow.state != next_state:
            return transition(
                flow_id,
                next_state,
                event="reconcile",
                payload={"child_states": sorted(states)},
            )
        return flow

    # Some children still running — make sure flow reflects that.
    if flow.state == "queued":
        return transition(
            flow_id,
            "running",
            event="reconcile",
            payload={"reason": "found in-flight children after restart"},
        )
    return flow


def attach_existing_tasks(
    flow_id: int,
    task_specs: Iterable[tuple[int, str | None]],
    relationship: str = "mirrored",
) -> list[FlowTask]:
    """Bulk-attach pre-existing tasks (mirrored relationship by default)."""
    out: list[FlowTask] = []
    for task_id, role in task_specs:
        out.append(add_child(flow_id, task_id, role=role, relationship=relationship))
    return out
