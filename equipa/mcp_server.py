"""EQUIPA MCP Server — JSON-RPC 2.0 over stdio.

Model Context Protocol server exposing EQUIPA orchestrator via MCP tools.
Uses ONLY Python stdlib (json, sys, subprocess) — no external dependencies.

Implements:
- JSON-RPC 2.0 protocol over stdio
- MCP initialization handshake
- 7 tools: dispatch, task_status, task_create, lessons, agent_logs, project_context, session_notes

Stderr is used for logging only. Stdout is reserved for JSON-RPC messages.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

# Import constants and DB helper
try:
    from equipa.constants import THEFORGE_DB
    from equipa.tasks import fetch_project_context
except ImportError:
    # Fallback if running as standalone
    THEFORGE_DB = Path(__file__).parent.parent / "theforge.db"

    def fetch_project_context(project_id: int) -> dict:
        """Minimal fallback for fetch_project_context."""
        return {"error": "fetch_project_context not available in standalone mode"}


def _log(msg: str) -> None:
    """Log to stderr only — never corrupt stdout."""
    print(f"[MCP] {msg}", file=sys.stderr, flush=True)


def _send_response(response: dict) -> None:
    """Send JSON-RPC response to stdout."""
    json.dump(response, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _send_error(request_id: int | str | None, code: int, message: str) -> None:
    """Send JSON-RPC error response."""
    _send_response({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        }
    })


def _get_db_connection() -> sqlite3.Connection:
    """Get connection to TheForge DB."""
    db_path = THEFORGE_DB
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# --- MCP Tool Handlers ---

def _handle_equipa_dispatch(args: dict) -> dict:
    """Spawn orchestrator subprocess for a task.

    Args:
        task_id (int): Task ID to dispatch
        role (str, optional): Agent role (default: developer)
        max_turns (int, optional): Max turns
        model (str, optional): Model override

    Returns:
        dict: {"status": "spawned", "pid": int}
    """
    task_id = args.get("task_id")
    if not task_id:
        return {"error": "task_id required"}

    role = args.get("role", "developer")
    max_turns = args.get("max_turns")
    model = args.get("model")

    # Build command
    cmd = [sys.executable, "-m", "equipa.cli", "--task", str(task_id), "--role", role, "--yes"]
    if max_turns:
        cmd.extend(["--max-turns", str(max_turns)])
    if model:
        cmd.extend(["--model", model])

    _log(f"Spawning: {' '.join(cmd)}")

    # Spawn detached subprocess
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    return {
        "status": "spawned",
        "pid": proc.pid,
        "task_id": task_id,
        "role": role,
    }


def _handle_equipa_task_status(args: dict) -> dict:
    """Query task status from DB.

    Args:
        task_id (int): Task ID

    Returns:
        dict: Task record including status, title, description, etc.
    """
    task_id = args.get("task_id")
    if not task_id:
        return {"error": "task_id required"}

    conn = _get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT t.*, p.name as project_name
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.id = ?
            """,
            (task_id,),
        ).fetchone()

        if not row:
            return {"error": f"Task {task_id} not found"}

        return dict(row)
    finally:
        conn.close()


def _handle_equipa_task_create(args: dict) -> dict:
    """Create a new task in TheForge.

    Args:
        project_id (int): Project ID
        title (str): Task title
        description (str): Task description
        priority (str, optional): Task priority (default: medium)

    Returns:
        dict: {"task_id": int, "status": "created"}
    """
    project_id = args.get("project_id")
    title = args.get("title")
    description = args.get("description", "")
    priority = args.get("priority", "medium")

    if not project_id or not title:
        return {"error": "project_id and title required"}

    conn = _get_db_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO tasks (project_id, title, description, priority, status)
            VALUES (?, ?, ?, ?, 'todo')
            """,
            (project_id, title, description, priority),
        )
        conn.commit()
        task_id = cursor.lastrowid

        return {
            "task_id": task_id,
            "status": "created",
            "project_id": project_id,
            "title": title,
        }
    finally:
        conn.close()


def _handle_equipa_lessons(args: dict) -> dict:
    """Query lessons_learned table.

    Args:
        limit (int, optional): Max lessons to return (default: 20)
        error_type (str, optional): Filter by error type

    Returns:
        dict: {"lessons": [{"lesson": str, "error_type": str, ...}, ...]}
    """
    limit = args.get("limit", 20)
    error_type = args.get("error_type")

    conn = _get_db_connection()
    try:
        if error_type:
            rows = conn.execute(
                """
                SELECT lesson, error_type, error_signature, times_seen, created_at
                FROM lessons_learned
                WHERE error_type LIKE ?
                ORDER BY times_seen DESC, created_at DESC
                LIMIT ?
                """,
                (f"%{error_type}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT lesson, error_type, error_signature, times_seen, created_at
                FROM lessons_learned
                ORDER BY times_seen DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return {
            "lessons": [dict(r) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


def _handle_equipa_agent_logs(args: dict) -> dict:
    """Query agent_runs table.

    Args:
        task_id (int, optional): Filter by task ID
        limit (int, optional): Max runs to return (default: 10)

    Returns:
        dict: {"runs": [{"task_id": int, "role": str, "outcome": str, ...}, ...]}
    """
    task_id = args.get("task_id")
    limit = args.get("limit", 10)

    conn = _get_db_connection()
    try:
        if task_id:
            rows = conn.execute(
                """
                SELECT task_id, role, outcome, duration_seconds, created_at
                FROM agent_runs
                WHERE task_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT task_id, role, outcome, duration_seconds, created_at
                FROM agent_runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return {
            "runs": [dict(r) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


def _handle_equipa_project_context(args: dict) -> dict:
    """Fetch project context using tasks.fetch_project_context.

    Args:
        project_id (int): Project ID

    Returns:
        dict: Project context (session notes, open questions, decisions)
    """
    project_id = args.get("project_id")
    if not project_id:
        return {"error": "project_id required"}

    try:
        context = fetch_project_context(project_id)
        return context
    except Exception as exc:
        return {"error": f"Failed to fetch context: {exc}"}


def _handle_equipa_session_notes(args: dict) -> dict:
    """Query session_notes table.

    Args:
        project_id (int, optional): Filter by project ID
        limit (int, optional): Max notes to return (default: 5)

    Returns:
        dict: {"notes": [{"project_id": int, "summary": str, ...}, ...]}
    """
    project_id = args.get("project_id")
    limit = args.get("limit", 5)

    conn = _get_db_connection()
    try:
        if project_id:
            rows = conn.execute(
                """
                SELECT project_id, summary, next_steps, session_date
                FROM session_notes
                WHERE project_id = ?
                ORDER BY session_date DESC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT project_id, summary, next_steps, session_date
                FROM session_notes
                ORDER BY session_date DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return {
            "notes": [dict(r) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


# --- Tool Registry ---

TOOLS = {
    "equipa_dispatch": {
        "description": "Spawn EQUIPA orchestrator subprocess for a task",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID to dispatch"},
                "role": {"type": "string", "description": "Agent role (default: developer)", "default": "developer"},
                "max_turns": {"type": "integer", "description": "Max turns"},
                "model": {"type": "string", "description": "Model override"},
            },
            "required": ["task_id"],
        },
        "handler": _handle_equipa_dispatch,
    },
    "equipa_task_status": {
        "description": "Query task status from TheForge DB",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID"},
            },
            "required": ["task_id"],
        },
        "handler": _handle_equipa_task_status,
    },
    "equipa_task_create": {
        "description": "Create a new task in TheForge",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project ID"},
                "title": {"type": "string", "description": "Task title"},
                "description": {"type": "string", "description": "Task description"},
                "priority": {"type": "string", "description": "Priority (default: medium)", "default": "medium"},
            },
            "required": ["project_id", "title"],
        },
        "handler": _handle_equipa_task_create,
    },
    "equipa_lessons": {
        "description": "Query lessons_learned table",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max lessons (default: 20)", "default": 20},
                "error_type": {"type": "string", "description": "Filter by error type"},
            },
        },
        "handler": _handle_equipa_lessons,
    },
    "equipa_agent_logs": {
        "description": "Query agent_runs table",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Filter by task ID"},
                "limit": {"type": "integer", "description": "Max runs (default: 10)", "default": 10},
            },
        },
        "handler": _handle_equipa_agent_logs,
    },
    "equipa_project_context": {
        "description": "Fetch project context (session notes, open questions, decisions)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project ID"},
            },
            "required": ["project_id"],
        },
        "handler": _handle_equipa_project_context,
    },
    "equipa_session_notes": {
        "description": "Query session_notes table",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Filter by project ID"},
                "limit": {"type": "integer", "description": "Max notes (default: 5)", "default": 5},
            },
        },
        "handler": _handle_equipa_session_notes,
    },
}


# --- JSON-RPC Handlers ---

def _handle_initialize(params: dict, request_id: int | str) -> None:
    """Handle initialize request."""
    _log(f"Received initialize: {params}")

    # MCP initialization response
    _send_response({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": "equipa-mcp-server",
                "version": "1.0.0",
            },
        }
    })


def _handle_initialized(params: dict) -> None:
    """Handle initialized notification."""
    _log("Received initialized notification")
    # No response for notifications


def _handle_tools_list(params: dict, request_id: int | str) -> None:
    """Handle tools/list request."""
    _log("Received tools/list")

    tools_list = []
    for name, spec in TOOLS.items():
        tools_list.append({
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["inputSchema"],
        })

    _send_response({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "tools": tools_list,
        }
    })


def _handle_tools_call(params: dict, request_id: int | str) -> None:
    """Handle tools/call request."""
    tool_name = params.get("name")
    args = params.get("arguments", {})

    _log(f"Received tools/call: {tool_name} with args {args}")

    if tool_name not in TOOLS:
        _send_error(request_id, -32601, f"Unknown tool: {tool_name}")
        return

    try:
        handler = TOOLS[tool_name]["handler"]
        result = handler(args)

        _send_response({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, indent=2),
                    }
                ],
            }
        })
    except Exception as exc:
        _log(f"Tool error: {exc}")
        _send_error(request_id, -32603, f"Tool execution failed: {exc}")


# --- Main Loop ---

def run_server() -> None:
    """Main MCP server loop — read JSON-RPC from stdin, respond on stdout."""
    _log("EQUIPA MCP Server starting...")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            _log(f"Invalid JSON: {exc}")
            _send_error(None, -32700, "Parse error")
            continue

        # Extract fields
        jsonrpc = request.get("jsonrpc")
        method = request.get("method")
        params = request.get("params", {})
        request_id = request.get("id")

        if jsonrpc != "2.0":
            _send_error(request_id, -32600, "Invalid Request")
            continue

        # Dispatch based on method
        if method == "initialize":
            _handle_initialize(params, request_id)
        elif method == "notifications/initialized":
            _handle_initialized(params)
        elif method == "tools/list":
            _handle_tools_list(params, request_id)
        elif method == "tools/call":
            _handle_tools_call(params, request_id)
        else:
            _send_error(request_id, -32601, f"Method not found: {method}")


def main() -> None:
    """Entry point for MCP server."""
    try:
        run_server()
    except KeyboardInterrupt:
        _log("Server interrupted")
        sys.exit(0)
    except Exception as exc:
        _log(f"Fatal error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
