"""
ForgeTeam Phase 5: Multi-Project Orchestration with Resource Allocation

Picks a task from TheForge, spawns Developer and Tester agents via claude -p,
gives them MCP access to TheForge, and iterates until tests pass or max cycles hit.

Manager mode: Provide a natural-language goal and the system plans tasks,
executes them via Dev+Tester loops, evaluates results, and iterates.

Parallel goals mode: Run multiple Manager loops across different projects
concurrently from a goals JSON file.

Auto-run mode: Automatically scan all projects for pending work, prioritize
them by task priority weights, and dispatch Dev+Test loops concurrently.

Setup repos mode: Initialize git and create GitHub private repos for projects.

Usage:
    python forge_orchestrator.py --task 63
    python forge_orchestrator.py --task 63 --dev-test
    python forge_orchestrator.py --project 21 --dev-test
    python forge_orchestrator.py --goal "Add a --version flag" --goal-project 21
    python forge_orchestrator.py --goal "Add dark mode" --goal-project 4 --max-rounds 2
    python forge_orchestrator.py --parallel-goals goals.json
    python forge_orchestrator.py --parallel-goals goals.json --max-concurrent 2 --dry-run
    python forge_orchestrator.py --auto-run --dry-run
    python forge_orchestrator.py --auto-run --only-project 21 --dry-run
    python forge_orchestrator.py --auto-run --max-tasks-per-project 1 --yes
    python forge_orchestrator.py --auto-run --dispatch-config custom.json --yes
    python forge_orchestrator.py --setup-repos --dry-run
    python forge_orchestrator.py --setup-repos-project 21
    python forge_orchestrator.py --task 63 --dry-run
    python forge_orchestrator.py --task 63 --model opus --max-turns 50

Copyright 2026 Forgeborn
"""

import argparse
import asyncio
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# Force unbuffered output so logs are visible in real-time via nohup/SSH
os.environ["PYTHONUNBUFFERED"] = "1"


# --- Constants ---

# Default DB path — override via forge_config.json (run itzamna_setup.py to generate)
THEFORGE_DB = Path(__file__).parent / "theforge.db"
MCP_CONFIG = Path(__file__).parent / "mcp_config.json"
PROMPTS_DIR = Path(__file__).parent / "prompts"
SKILLS_DIR = Path(__file__).parent / "skills" / "security"

# Role prompt files (prepended with _common.md automatically)
ROLE_PROMPTS = {
    "developer": PROMPTS_DIR / "developer.md",
    "security-reviewer": PROMPTS_DIR / "security-reviewer.md",
    "tester": PROMPTS_DIR / "tester.md",
    "planner": PROMPTS_DIR / "planner.md",
    "evaluator": PROMPTS_DIR / "evaluator.md",
}

DEFAULT_MODEL = "sonnet"
DEFAULT_MAX_TURNS = 25
DEFAULT_MAX_RETRIES = 3
PROCESS_TIMEOUT = 1200  # 20 minutes

# Per-role turn limits (used when dispatch config or CLI doesn't specify)
DEFAULT_ROLE_TURNS = {
    "developer": 50,
    "tester": 20,
    "security-reviewer": 40,
    "planner": 25,
    "evaluator": 25,
}

# Checkpoint/Resume: save agent output on timeout for continuation
CHECKPOINT_DIR = Path(__file__).parent / ".forge-checkpoints"

# Complexity multipliers applied to per-role turn limits
COMPLEXITY_MULTIPLIERS = {
    "simple": 0.5,
    "medium": 1.0,
    "complex": 1.5,
    "epic": 2.0,
}

# Default model per role (overridden by dispatch_config)
DEFAULT_ROLE_MODELS = {
    "developer": "sonnet",
    "tester": "sonnet",
    "security-reviewer": "sonnet",
    "planner": "sonnet",
    "evaluator": "sonnet",
}

# Dev+Tester loop constants
MAX_DEV_TEST_CYCLES = 5
DEV_COMPACTION_THRESHOLD = 10    # turns before compacting developer
TESTER_COMPACTION_THRESHOLD = 6  # turns before compacting tester
NO_PROGRESS_LIMIT = 2            # consecutive no-change runs before blocking
MAX_CONTINUATIONS = 3            # auto-retries when developer runs out of turns/timeout

# Manager mode constants (Phase 3)
MAX_MANAGER_ROUNDS = 3       # max plan-execute-evaluate rounds
MAX_TASKS_PER_PLAN = 8       # planner can't create more than this
MAX_FOLLOWUP_TASKS = 4       # evaluator can't create more than this per round

# Project codenames mapped to their directories.
# Empty by default — populated from forge_config.json (run itzamna_setup.py first,
# then use --add-project to register projects).
PROJECT_DIRS = {}

# For sorting text-based priority values
PRIORITY_ORDER = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}

# Default GitHub owner for --setup-repos
GITHUB_OWNER = ""


# --- Portable Configuration ---

def load_config():
    """Load forge_config.json if present alongside this script.

    Overrides THEFORGE_DB, PROJECT_DIRS, GITHUB_OWNER, MCP_CONFIG, and
    PROMPTS_DIR with values from the config file.  Falls back silently to
    the hardcoded defaults above when no config file exists.
    """
    global THEFORGE_DB, PROJECT_DIRS, GITHUB_OWNER, MCP_CONFIG, PROMPTS_DIR

    config_path = Path(__file__).parent / "forge_config.json"
    if not config_path.exists():
        return  # backward compatible — use hardcoded values

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: Failed to read {config_path}: {exc}")
        return

    if "theforge_db" in cfg:
        THEFORGE_DB = Path(cfg["theforge_db"])
    if "project_dirs" in cfg:
        PROJECT_DIRS = {k.lower(): v for k, v in cfg["project_dirs"].items()}
    if "github_owner" in cfg:
        GITHUB_OWNER = cfg["github_owner"]
    if "mcp_config" in cfg:
        MCP_CONFIG = Path(cfg["mcp_config"])
    if "prompts_dir" in cfg:
        PROMPTS_DIR = Path(cfg["prompts_dir"])


def _discover_roles():
    """Dynamically build ROLE_PROMPTS from .md files in the prompts directory.

    Scans PROMPTS_DIR for markdown files (excluding _common.md) and maps
    each filename stem to its full path.  Falls back to the hardcoded
    ROLE_PROMPTS dict if the prompts directory doesn't exist.
    """
    global ROLE_PROMPTS

    if not PROMPTS_DIR.exists():
        return  # keep hardcoded dict

    discovered = {}
    for md_file in sorted(PROMPTS_DIR.glob("*.md")):
        if md_file.name.startswith("_"):
            continue  # skip _common.md and similar
        role_name = md_file.stem  # e.g. "developer", "security-reviewer"
        discovered[role_name] = md_file

    if discovered:
        ROLE_PROMPTS = discovered


def _handle_add_project(name, project_dir):
    """Register a new project in the Itzamna DB and update forge_config.json."""
    project_dir = str(Path(project_dir).resolve())

    # Insert into DB
    if not THEFORGE_DB.exists():
        print(f"ERROR: Database not found at {THEFORGE_DB}")
        sys.exit(1)

    conn = sqlite3.connect(str(THEFORGE_DB))
    try:
        codename = name.lower().replace(" ", "")
        conn.execute(
            "INSERT INTO projects (name, codename, status) VALUES (?, ?, 'active')",
            (name, codename),
        )
        conn.commit()
        project_id = conn.execute(
            "SELECT id FROM projects WHERE codename = ?", (codename,)
        ).fetchone()[0]
        print(f"Created project '{name}' (codename: {codename}, id: {project_id})")
    except sqlite3.IntegrityError:
        print(f"ERROR: Project '{name}' already exists in TheForge")
        conn.close()
        sys.exit(1)
    finally:
        conn.close()

    # Update forge_config.json if it exists
    config_path = Path(__file__).parent / "forge_config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg.setdefault("project_dirs", {})[codename] = project_dir
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4)
            print(f"Updated {config_path} with project directory: {project_dir}")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"WARNING: Could not update config: {exc}")
    else:
        print(f"NOTE: No forge_config.json found. Add project dir to PROJECT_DIRS manually.")

    print(f"\nProject '{name}' registered successfully.")
    print(f"  ID: {project_id}")
    print(f"  Dir: {project_dir}")


# Apply config and discover roles at module load time
load_config()
_discover_roles()

# .gitignore templates for --setup-repos
GITIGNORE_TEMPLATES = {
    "python": "\n".join([
        "__pycache__/", "*.pyc", "*.pyo", "*.egg-info/", "dist/", "build/",
        ".eggs/", "*.egg", ".venv/", "venv/", ".env", "*.db",
        "nul", "tmpclaude-*", "~$*", "desktop.ini", ".DS_Store", "Thumbs.db",
    ]),
    "dotnet": "\n".join([
        "bin/", "obj/", ".vs/", "*.user", "*.suo", "*.cache",
        "packages/", "*.nupkg", ".env", "*.db",
        "publish/", "**/Debug/", "**/Release/",
        "nul", "tmpclaude-*", "~$*", "desktop.ini", ".DS_Store", "Thumbs.db",
    ]),
    "node": "\n".join([
        "node_modules/", "dist/", ".env", "*.db",
        "npm-debug.log*", "yarn-error.log*",
        "nul", "tmpclaude-*", "~$*", "desktop.ini", ".DS_Store", "Thumbs.db",
    ]),
    "default": "\n".join([
        "__pycache__/", "*.pyc", "*.pyo",
        "bin/", "obj/", ".vs/", "*.user",
        "node_modules/", "dist/", "build/",
        ".env", "*.db", ".venv/", "venv/",
        "nul", "tmpclaude-*", "~$*", "desktop.ini", ".DS_Store", "Thumbs.db",
    ]),
}


# --- Output Helper ---

def log(msg, output=None):
    """Print a message or buffer it for later display.

    In single-goal mode, output is None and messages print immediately.
    In parallel mode, output is a list and messages are collected for
    display after the goal completes.
    """
    if output is not None:
        output.append(msg)
    else:
        print(msg)


# --- Database Functions ---

def get_db_connection(write=False):
    """Open a connection to TheForge database.

    Args:
        write: If True, open in read-write mode. Default is read-only.
    """
    if not THEFORGE_DB.exists():
        print(f"ERROR: TheForge database not found at {THEFORGE_DB}")
        sys.exit(1)

    if write:
        conn = sqlite3.connect(str(THEFORGE_DB))
    else:
        uri = f"file:{THEFORGE_DB}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def update_task_status(task_id, outcome, output=None):
    """Update task status in TheForge based on dev-test outcome.

    Called by the orchestrator after run_dev_test_loop completes, so agents
    don't need to handle DB updates themselves (they often run out of turns).

    Maps outcomes to statuses:
        tests_passed, no_tests -> done
        Everything else (blocked, failed, timeout, no_progress) -> blocked
    """
    success_outcomes = ("tests_passed", "no_tests")
    new_status = "done" if outcome in success_outcomes else "blocked"

    # Orchestrator is authoritative — always set status based on outcome
    conn = get_db_connection(write=True)
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            log(f"  [DB] Task {task_id} not found — skipping status update.", output)
            return
        current = row["status"]

        conn.execute(
            "UPDATE tasks SET status = ?, completed_at = CASE WHEN ? = 'done' THEN datetime('now') ELSE completed_at END WHERE id = ?",
            (new_status, new_status, task_id),
        )
        conn.commit()
        log(f"  [DB] Task {task_id}: {current} -> {new_status} (outcome: {outcome})", output)
    except Exception as e:
        log(f"  [DB] ERROR updating task {task_id}: {e}", output)
    finally:
        conn.close()


def fetch_task(task_id):
    """Fetch a specific task by ID, including project info."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT t.*, p.name as project_name,
                   COALESCE(p.codename, LOWER(REPLACE(p.name, ' ', ''))) as project_codename
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.id = ?
            """,
            (task_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def fetch_next_todo(project_id):
    """Find the highest-priority todo task for a project."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT t.*, p.name as project_name,
                   COALESCE(p.codename, LOWER(REPLACE(p.name, ' ', ''))) as project_codename
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.project_id = ? AND t.status = 'todo'
            ORDER BY t.created_at ASC
            """,
            (project_id,),
        ).fetchall()

        if not rows:
            return None

        # Sort by priority text mapping (critical > high > medium > low)
        tasks = [dict(r) for r in rows]
        tasks.sort(
            key=lambda t: PRIORITY_ORDER.get(
                str(t.get("priority", "low")).lower(), 0
            ),
            reverse=True,
        )
        return tasks[0]
    finally:
        conn.close()


def fetch_project_context(project_id):
    """Get recent project context: last session, open questions, recent decisions."""
    conn = get_db_connection()
    try:
        context = {}

        # Last session notes
        row = conn.execute(
            """
            SELECT summary, next_steps, session_date
            FROM session_notes
            WHERE project_id = ?
            ORDER BY session_date DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        context["last_session"] = dict(row) if row else None

        # Open questions
        rows = conn.execute(
            """
            SELECT question, context
            FROM open_questions
            WHERE project_id = ? AND resolved = 0
            """,
            (project_id,),
        ).fetchall()
        context["open_questions"] = [dict(r) for r in rows]

        # Recent decisions
        rows = conn.execute(
            """
            SELECT decision, rationale, decided_at
            FROM decisions
            WHERE project_id = ?
            ORDER BY decided_at DESC
            LIMIT 5
            """,
            (project_id,),
        ).fetchall()
        context["recent_decisions"] = [dict(r) for r in rows]

        return context
    finally:
        conn.close()


def _get_task_status(task_id):
    """Quick read of task status string from DB."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


def fetch_project_info(project_id):
    """Get project name and codename from project_id."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT id, name,
                   COALESCE(codename, LOWER(REPLACE(name, ' ', ''))) as codename
            FROM projects
            WHERE id = ?
            """,
            (project_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def fetch_tasks_by_ids(task_ids):
    """Fetch multiple tasks by their IDs.

    Returns a list of task dicts in the same order as the input IDs.
    Missing tasks are skipped.
    """
    if not task_ids:
        return []

    conn = get_db_connection()
    try:
        placeholders = ", ".join("?" for _ in task_ids)
        rows = conn.execute(
            f"""
            SELECT t.*, p.name as project_name,
                   COALESCE(p.codename, LOWER(REPLACE(p.name, ' ', ''))) as project_codename
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.id IN ({placeholders})
            """,
            task_ids,
        ).fetchall()

        # Build a dict keyed by ID for ordering
        by_id = {row["id"]: dict(row) for row in rows}
        return [by_id[tid] for tid in task_ids if tid in by_id]
    finally:
        conn.close()


# --- Prompt Building ---

def build_task_prompt(task, project_context, project_dir):
    """Build the task-specific instruction block."""
    # Task metadata (safe — controlled by orchestrator, not user input)
    lines = [
        "## Assigned Task",
        f"- **Task ID:** {task['id']}",
        f"- **Project:** {task.get('project_name', 'Unknown')} (project_id: {task.get('project_id', '?')})",
        f"- **Priority:** {task.get('priority', 'medium')}",
        f"- **Working Directory:** {project_dir}",
        "",
    ]

    # Task title and description wrapped in isolation tags (security finding #4)
    # These come from the database and could contain injection attempts
    lines.append('<task-input type="task-title" trust="database">')
    lines.append(task["title"])
    lines.append("</task-input>")
    lines.append("")
    lines.append('<task-input type="task-description" trust="database">')
    lines.append(task.get("description", "No description provided"))
    lines.append("</task-input>")
    lines.append("")

    # Project context also wrapped — comes from database
    session = project_context.get("last_session")
    if session:
        lines.append("## Recent Project Context")
        lines.append('<task-input type="session-context" trust="database">')
        lines.append(f"Last session ({session.get('session_date', 'unknown')}):")
        lines.append(session.get("summary", "No summary"))
        if session.get("next_steps"):
            lines.append(f"Next steps: {session['next_steps']}")
        lines.append("</task-input>")
        lines.append("")

    questions = project_context.get("open_questions", [])
    if questions:
        lines.append("## Open Questions (unresolved)")
        lines.append('<task-input type="open-questions" trust="database">')
        for q in questions:
            lines.append(f"- {q['question']}")
            if q.get("context"):
                lines.append(f"  Context: {q['context']}")
        lines.append("</task-input>")
        lines.append("")

    decisions = project_context.get("recent_decisions", [])
    if decisions:
        lines.append("## Recent Decisions")
        lines.append('<task-input type="decisions" trust="database">')
        for d in decisions:
            lines.append(f"- {d['decision']} ({d.get('decided_at', 'unknown')})")
            if d.get("rationale"):
                lines.append(f"  Rationale: {d['rationale']}")
        lines.append("</task-input>")
        lines.append("")

    return "\n".join(lines)


def build_system_prompt(task, project_context, project_dir, role="developer",
                        extra_context=""):
    """Read _common.md + role prompt, replace placeholders, append task prompt.

    extra_context: optional string appended after the task prompt (used for
    compaction history and test failure feedback in dev-test loop).
    """
    common_path = PROMPTS_DIR / "_common.md"
    role_path = ROLE_PROMPTS.get(role)

    if not role_path:
        print(f"ERROR: Unknown role '{role}'. Available: {', '.join(ROLE_PROMPTS.keys())}")
        sys.exit(1)

    if not common_path.exists():
        print(f"ERROR: Common prompt not found at {common_path}")
        sys.exit(1)

    if not role_path.exists():
        print(f"ERROR: Role prompt not found at {role_path}")
        sys.exit(1)

    # Build prompt: common rules + role-specific prompt
    common_text = common_path.read_text(encoding="utf-8")
    role_text = role_path.read_text(encoding="utf-8")
    template = common_text + "\n\n---\n\n" + role_text

    # Replace placeholders
    prompt = template.replace("{task_id}", str(task["id"]))
    prompt = prompt.replace("{project_id}", str(task.get("project_id", "")))

    # Append task-specific instructions
    task_prompt = build_task_prompt(task, project_context, project_dir)
    prompt = prompt + "\n\n---\n\n" + task_prompt

    # Append extra context (compaction history, test failures) if provided
    if extra_context:
        prompt = prompt + "\n\n---\n\n" + extra_context

    return prompt


def get_task_complexity(task):
    """Resolve task complexity.

    Checks the task's 'complexity' field first (set in DB), then infers from
    description length as a fallback.

    Returns one of: 'simple', 'medium', 'complex', 'epic'
    """
    # Explicit complexity in the task record
    explicit = ((task or {}).get("complexity") or "").strip().lower()
    if explicit in COMPLEXITY_MULTIPLIERS:
        return explicit

    # Infer from description length
    desc = (task or {}).get("description", "") or ""
    desc_len = len(desc)
    if desc_len < 100:
        return "simple"
    elif desc_len < 400:
        return "medium"
    elif desc_len < 800:
        return "complex"
    else:
        return "epic"


def get_role_turns(role, args, config=None, task=None):
    """Resolve max turns for a given role, adjusted by task complexity.

    Priority: dispatch config per-role > CLI --max-turns (if non-default) > DEFAULT_ROLE_TURNS
    Then applies complexity multiplier from the task.
    """
    # Check dispatch config for per-role overrides (e.g. "max_turns_developer": 50)
    effective_config = config or getattr(args, "dispatch_config", None)
    base_turns = None
    if effective_config:
        role_key = role.replace("-", "_")  # security-reviewer -> security_reviewer
        config_key = f"max_turns_{role_key}"
        if config_key in effective_config:
            base_turns = effective_config[config_key]

    if base_turns is None:
        # If CLI specified a non-default value, use it for all roles
        cli_turns = getattr(args, "max_turns", DEFAULT_MAX_TURNS)
        if cli_turns != DEFAULT_MAX_TURNS:
            base_turns = cli_turns
        else:
            # Fall back to per-role defaults
            base_turns = DEFAULT_ROLE_TURNS.get(role, DEFAULT_MAX_TURNS)

    # Apply complexity multiplier
    if task:
        complexity = get_task_complexity(task)
        multiplier = COMPLEXITY_MULTIPLIERS.get(complexity, 1.0)
        adjusted = int(base_turns * multiplier)
        # Enforce minimum of 10 turns
        return max(10, adjusted)

    return base_turns


def get_role_model(role, args, config=None, task=None):
    """Resolve model for a given role and task complexity.

    Priority:
      1. dispatch config per-complexity (e.g. model_epic, model_complex)
      2. dispatch config per-role (e.g. model_developer, model_tester)
      3. CLI --model
      4. dispatch config global model
      5. DEFAULT_ROLE_MODELS
    """
    effective_config = config or getattr(args, "dispatch_config", None)

    if effective_config and task:
        # Check complexity-based model override
        complexity = get_task_complexity(task)
        complexity_key = f"model_{complexity}"
        if complexity_key in effective_config:
            return effective_config[complexity_key]

    if effective_config:
        # Check role-based model override
        role_key = role.replace("-", "_")
        role_model_key = f"model_{role_key}"
        if role_model_key in effective_config:
            return effective_config[role_model_key]

    # CLI override
    cli_model = getattr(args, "model", DEFAULT_MODEL)
    if cli_model != DEFAULT_MODEL:
        return cli_model

    # Config global model
    if effective_config and "model" in effective_config:
        return effective_config["model"]

    return DEFAULT_ROLE_MODELS.get(role, DEFAULT_MODEL)


# --- Checkpoint/Resume ---

def save_checkpoint(task_id, attempt, output_text, role="developer"):
    """Save agent output to a checkpoint file for resume on retry.

    Returns the checkpoint file path.
    """
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"task_{task_id}_{role}_attempt_{attempt}.txt"
    filepath = CHECKPOINT_DIR / filename
    try:
        filepath.write_text(output_text, encoding="utf-8")
    except OSError as e:
        print(f"  [Checkpoint] WARNING: Failed to save checkpoint: {e}")
        return None
    return filepath


def load_checkpoint(task_id, role="developer"):
    """Load the most recent checkpoint for a task+role.

    Returns (checkpoint_text, attempt_number) or (None, 0) if no checkpoint exists.
    """
    if not CHECKPOINT_DIR.exists():
        return None, 0

    # Find all checkpoints for this task+role, sorted by attempt number
    pattern = f"task_{task_id}_{role}_attempt_*.txt"
    checkpoints = sorted(CHECKPOINT_DIR.glob(pattern))
    if not checkpoints:
        return None, 0

    latest = checkpoints[-1]
    try:
        text = latest.read_text(encoding="utf-8")
    except OSError:
        return None, 0

    # Extract attempt number from filename
    stem = latest.stem  # e.g. task_124_developer_attempt_2
    try:
        attempt = int(stem.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        attempt = 0

    return text, attempt


def clear_checkpoints(task_id, role=None):
    """Remove checkpoint files for a completed task."""
    if not CHECKPOINT_DIR.exists():
        return
    if role:
        pattern = f"task_{task_id}_{role}_attempt_*.txt"
    else:
        pattern = f"task_{task_id}_*_attempt_*.txt"
    for f in CHECKPOINT_DIR.glob(pattern):
        try:
            f.unlink()
        except OSError:
            pass


def build_checkpoint_context(checkpoint_text, attempt):
    """Build context string from a checkpoint for the next agent attempt."""
    # Truncate very long checkpoints to avoid blowing up the prompt
    max_chars = 8000
    if len(checkpoint_text) > max_chars:
        checkpoint_text = (
            checkpoint_text[:max_chars]
            + f"\n\n[... truncated, {len(checkpoint_text) - max_chars} chars omitted ...]"
        )

    return (
        f"## Previous Attempt (#{attempt}) — Continue From Here\n\n"
        f"A previous agent worked on this task but ran out of turns or timed out. "
        f"Here is what they accomplished. **Do NOT repeat work that is already done.** "
        f"Pick up where they left off.\n\n"
        f"### Previous Agent Output:\n```\n{checkpoint_text}\n```\n\n"
        f"**IMPORTANT:** Review the project files to see what was already implemented. "
        f"Focus only on what remains to be done."
    )


# --- CLI Command Building ---

def build_cli_command(system_prompt, project_dir, max_turns, model, role="developer"):
    """Build the claude CLI command as a list of arguments."""
    cmd = [
        "claude",
        "-p",
        f"Execute the task described in your system prompt. Work in: {project_dir}",
        "--output-format", "json",
        "--model", model,
        "--max-turns", str(max_turns),
        "--no-session-persistence",
        "--append-system-prompt", system_prompt,
        "--mcp-config", str(MCP_CONFIG),
        "--add-dir", str(project_dir),
        "--permission-mode", "bypassPermissions",
    ]

    # Security reviewer gets access to the security skills directory
    if role == "security-reviewer" and SKILLS_DIR.exists():
        cmd.extend(["--add-dir", str(SKILLS_DIR)])

    return cmd


# --- Agent Execution ---

async def run_agent(cmd, timeout=None):
    """Spawn claude -p, capture output, handle timeout."""
    effective_timeout = timeout or PROCESS_TIMEOUT
    start_time = time.time()

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            # Try to capture any partial output before killing
            process.kill()
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=5,
                )
                partial_text = stdout_bytes.decode("utf-8", errors="replace").strip()
            except Exception:
                partial_text = ""
            duration = time.time() - start_time
            return {
                "success": False,
                "result_text": partial_text,
                "num_turns": 0,
                "duration": duration,
                "cost": None,
                "errors": [f"Process timed out after {effective_timeout} seconds"],
            }

    except FileNotFoundError:
        return {
            "success": False,
            "result_text": "",
            "num_turns": 0,
            "duration": 0,
            "cost": None,
            "errors": ["'claude' command not found. Is Claude Code installed and on PATH?"],
        }

    duration = time.time() - start_time
    stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

    # Parse JSON output
    result = {
        "success": False,
        "result_text": stdout_text,
        "num_turns": 0,
        "duration": duration,
        "cost": None,
        "errors": [],
    }

    if stderr_text:
        result["errors"].append(f"stderr: {stderr_text}")

    if not stdout_text:
        result["errors"].append("No output from agent")
        return result

    try:
        data = json.loads(stdout_text)
        result["result_text"] = data.get("result", stdout_text)
        result["num_turns"] = data.get("num_turns", 0)
        result["cost"] = data.get("cost_usd")

        # Check for error subtypes
        subtype = data.get("subtype", "")
        if subtype == "error_max_turns":
            # Agent ran out of turns but may have done useful work
            result["success"] = True
            result["errors"].append("Agent hit max turns limit")
        elif data.get("is_error"):
            result["success"] = False
            result["errors"].append(f"Agent error: {data.get('result', 'unknown')}")
        else:
            result["success"] = True

    except json.JSONDecodeError:
        # Output wasn't JSON, treat raw text as result
        result["result_text"] = stdout_text
        result["success"] = process.returncode == 0

    return result


# --- Circuit Breakers ---

def validate_output(result):
    """Check if agent output contains the expected structured response.

    Returns (is_valid, reason) tuple.
    """
    if not result["success"]:
        return False, "Agent reported failure"

    text = result.get("result_text", "")
    if not text:
        return False, "No output from agent"

    # Check for the structured RESULT: marker
    if "RESULT:" in text:
        return True, "Structured output found"

    # Agent might have done useful work without the marker
    # (e.g., hit max turns). Check if there's substantial output.
    if len(text) > 100:
        return True, "Substantial output (no RESULT marker)"

    return False, "Output too short and missing RESULT marker"


async def run_agent_with_retries(cmd, task, max_retries):
    """Run agent with retry logic on failure.

    Returns (result, attempt_number) tuple.
    """
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            print(f"\n--- Retry {attempt}/{max_retries} ---")

        result = await run_agent(cmd)

        # Check if output is valid
        is_valid, reason = validate_output(result)

        if is_valid:
            return result, attempt

        print(f"  Attempt {attempt} failed: {reason}")

        # Check if the agent updated the task to blocked — that's intentional, not a failure
        verified, _ = verify_task_updated(task["id"])
        if verified:
            return result, attempt

        # Don't retry on timeout — the task is probably too complex
        if any("timed out" in e for e in result.get("errors", [])):
            print("  Not retrying: process timed out")
            return result, attempt

    print(f"\n  All {max_retries} attempts failed.")
    return result, max_retries


# --- Output Parsers (Phase 2) ---

def parse_tester_output(result_text):
    """Parse structured output from the Tester agent.

    Looks for RESULT, TEST_FRAMEWORK, TESTS_RUN, TESTS_FAILED,
    FAILURE_DETAILS, and RECOMMENDATIONS lines.

    Returns a dict with parsed fields.
    """
    parsed = {
        "result": "unknown",
        "test_framework": "none",
        "tests_run": 0,
        "tests_passed": 0,
        "tests_failed": 0,
        "failure_details": [],
        "recommendations": [],
        "summary": "",
    }

    if not result_text:
        return parsed

    lines = result_text.splitlines()
    current_section = None  # tracks multi-line sections

    for line in lines:
        stripped = line.strip()

        # Single-value fields
        if stripped.startswith("RESULT:"):
            parsed["result"] = stripped.split(":", 1)[1].strip().lower()
            current_section = None
        elif stripped.startswith("TEST_FRAMEWORK:"):
            parsed["test_framework"] = stripped.split(":", 1)[1].strip()
            current_section = None
        elif stripped.startswith("TESTS_RUN:"):
            try:
                parsed["tests_run"] = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass
            current_section = None
        elif stripped.startswith("TESTS_PASSED:"):
            try:
                parsed["tests_passed"] = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass
            current_section = None
        elif stripped.startswith("TESTS_FAILED:"):
            try:
                parsed["tests_failed"] = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass
            current_section = None
        elif stripped.startswith("SUMMARY:"):
            parsed["summary"] = stripped.split(":", 1)[1].strip()
            current_section = None

        # Multi-line sections
        elif stripped.startswith("FAILURE_DETAILS:"):
            current_section = "failure_details"
        elif stripped.startswith("RECOMMENDATIONS:"):
            current_section = "recommendations"

        # Collect bullet items for multi-line sections
        elif current_section and stripped.startswith("- "):
            item = stripped[2:].strip()
            if item.lower() != "none":
                parsed[current_section].append(item)

    return parsed


def parse_developer_output(result_text):
    """Extract FILES_CHANGED list from developer output.

    Looks for a FILES_CHANGED: section with bullet items.
    Returns a list of filenames.
    """
    files = []
    if not result_text:
        return files

    lines = result_text.splitlines()
    in_files_section = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("FILES_CHANGED:"):
            in_files_section = True
            # Check for inline value like "FILES_CHANGED: none"
            value = stripped.split(":", 1)[1].strip().lower()
            if value and value != "none":
                files.append(value)
            continue

        if in_files_section:
            if stripped.startswith("- "):
                item = stripped[2:].strip()
                if item.lower() != "none":
                    files.append(item)
            elif stripped and not stripped.startswith("-"):
                # End of the section (hit a non-bullet, non-empty line)
                in_files_section = False

    return files


# --- Session Compaction (Phase 2) ---

def build_compaction_summary(role, result, cycle, task):
    """Capture tail of agent output with cycle/role context.

    Used to inject prior work context into the next agent's prompt
    when an agent exceeds the turn threshold.
    """
    text = result.get("result_text", "")
    # Keep last 2000 chars to stay within prompt size limits
    tail = text[-2000:] if len(text) > 2000 else text

    summary = (
        f"## Prior Work Summary (Cycle {cycle}, {role})\n"
        f"Task: #{task['id']} - {task['title']}\n"
        f"Turns used: {result.get('num_turns', '?')}\n"
        f"---\n"
        f"{tail}\n"
    )
    return summary


def build_test_failure_context(test_results, cycle):
    """Format Tester failures + recommendations for the Developer's next attempt.

    Returns a string to append to the Developer's system prompt.
    """
    lines = [
        f"## Test Failures from Cycle {cycle}",
        "",
        f"The Tester agent ran {test_results['tests_run']} tests "
        f"using {test_results['test_framework']}.",
        f"**{test_results['tests_failed']} tests failed.**",
        "",
    ]

    if test_results["failure_details"]:
        lines.append("### Failing Tests:")
        for detail in test_results["failure_details"]:
            lines.append(f"- {detail}")
        lines.append("")

    if test_results["recommendations"]:
        lines.append("### Tester Recommendations:")
        for rec in test_results["recommendations"]:
            lines.append(f"- {rec}")
        lines.append("")

    lines.append("**Fix these test failures. Do NOT skip or delete failing tests.**")
    lines.append("")

    return "\n".join(lines)


async def auto_install_dependencies(project_dir, output=None):
    """Auto-install project dependencies if manifest exists but deps are missing.

    Checks for pyproject.toml/requirements.txt (Python) and package.json (Node).
    Runs install in background, logs result.
    """
    pdir = Path(project_dir)

    # Python: pyproject.toml or requirements.txt without venv
    has_pyproject = (pdir / "pyproject.toml").exists()
    has_requirements = (pdir / "requirements.txt").exists()
    has_venv = (pdir / "venv").exists() or (pdir / ".venv").exists()

    if (has_pyproject or has_requirements) and not has_venv:
        log(f"  [Auto-Install] Python project without venv detected. Installing...", output)
        try:
            venv_path = pdir / "venv"
            # Create venv
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "venv", str(venv_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            # Install deps
            pip_path = venv_path / "bin" / "pip"
            if not pip_path.exists():
                pip_path = venv_path / "Scripts" / "pip"  # Windows

            if has_pyproject:
                install_cmd = [str(pip_path), "install", "-e", f"{project_dir}[dev]"]
            else:
                install_cmd = [str(pip_path), "install", "-r", str(pdir / "requirements.txt")]

            proc = await asyncio.create_subprocess_exec(
                *install_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                log(f"  [Auto-Install] Python deps installed successfully.", output)
            else:
                err = stderr.decode("utf-8", errors="replace")[:200]
                log(f"  [Auto-Install] Python install failed (rc={proc.returncode}): {err}", output)
        except Exception as e:
            log(f"  [Auto-Install] Python install error: {e}", output)

    # Node.js: package.json without node_modules
    has_package_json = (pdir / "package.json").exists()
    has_node_modules = (pdir / "node_modules").exists()

    if has_package_json and not has_node_modules:
        log(f"  [Auto-Install] Node.js project without node_modules detected. Installing...", output)
        try:
            proc = await asyncio.create_subprocess_exec(
                "npm", "install",
                cwd=str(pdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                log(f"  [Auto-Install] Node.js deps installed successfully.", output)
            else:
                err = stderr.decode("utf-8", errors="replace")[:200]
                log(f"  [Auto-Install] npm install failed (rc={proc.returncode}): {err}", output)
        except FileNotFoundError:
            log(f"  [Auto-Install] npm not found. Skipping Node.js dep install.", output)
        except Exception as e:
            log(f"  [Auto-Install] npm install error: {e}", output)


# --- Dev+Tester Loop (Phase 2) ---

async def run_dev_test_loop(task, project_dir, project_context, args, output=None):
    """Run the Developer + Tester iteration loop.

    Flow per cycle:
    1. Check for checkpoint from a previous timed-out attempt
    2. Run Developer agent (with checkpoint + compaction/failure context)
    3. On timeout/max_turns -> save checkpoint for future resume
    4. Check if Developer marked task blocked -> exit
    5. Track FILES_CHANGED for progress detection
    6. Run Tester agent
    7. Parse Tester output:
       - pass -> clear checkpoints, exit success
       - no-tests -> clear checkpoints, exit accept
       - blocked -> exit
       - fail -> feed failures to next Developer cycle

    Returns (last_result, cycles_completed, outcome_reason) tuple.
    """
    # Auto-install deps before first cycle if needed
    await auto_install_dependencies(project_dir, output=output)

    compaction_history = []  # accumulated context across cycles
    no_progress_count = 0    # consecutive cycles with no FILES_CHANGED
    continuation_count = 0   # auto-retries when developer runs out of turns
    total_cost = 0.0
    total_duration = 0.0
    task_id = task["id"]

    # Reset status so orchestrator is authoritative
    conn = get_db_connection(write=True)
    conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    log(f"  [Setup] Task {task_id} status reset to in_progress (orchestrator manages lifecycle)", output)

    # Resolve model and turns using adaptive tiering
    complexity = get_task_complexity(task)
    dev_model = get_role_model("developer", args, task=task)
    tester_model = get_role_model("tester", args, task=task)
    dev_turns_limit = get_role_turns("developer", args, task=task)
    tester_turns_limit = get_role_turns("tester", args, task=task)

    log(f"  Task complexity: {complexity}", output)
    log(f"  Developer: model={dev_model}, max_turns={dev_turns_limit}", output)
    log(f"  Tester: model={tester_model}, max_turns={tester_turns_limit}", output)

    # Check for checkpoint from a previous timed-out attempt
    checkpoint_text, prev_attempt = load_checkpoint(task_id, role="developer")
    if checkpoint_text:
        checkpoint_context = build_checkpoint_context(checkpoint_text, prev_attempt)
        compaction_history.append(checkpoint_context)
        log(f"  [Checkpoint] Loaded checkpoint from attempt #{prev_attempt} "
            f"({len(checkpoint_text)} chars). Agent will continue from there.", output)

    for cycle in range(1, MAX_DEV_TEST_CYCLES + 1):
        log(f"\n{'=' * 50}", output)
        log(f"  DEV-TEST CYCLE {cycle}/{MAX_DEV_TEST_CYCLES}", output)
        log(f"{'=' * 50}", output)

        # --- Developer Phase ---
        log(f"\n  [Cycle {cycle}] Running Developer agent...", output)

        # Build extra context from compaction history
        extra_context = "\n\n".join(compaction_history) if compaction_history else ""

        dev_prompt = build_system_prompt(
            task, project_context, project_dir,
            role="developer", extra_context=extra_context,
        )
        dev_cmd = build_cli_command(
            dev_prompt, project_dir, dev_turns_limit, dev_model, role="developer",
        )

        dev_result = await run_agent(dev_cmd)
        total_duration += dev_result.get("duration", 0)
        if dev_result.get("cost"):
            total_cost += dev_result["cost"]

        # Check for timeout or max_turns — save checkpoint for resume
        is_timeout = any("timed out" in e for e in dev_result.get("errors", []))
        is_max_turns = any("max turns" in e for e in dev_result.get("errors", []))

        if is_timeout or is_max_turns:
            reason = "timed out" if is_timeout else "hit max turns"
            continuation_count += 1
            log(f"  [Cycle {cycle}] Developer {reason}. "
                f"(continuation {continuation_count}/{MAX_CONTINUATIONS})", output)

            # Save checkpoint
            result_text = dev_result.get("result_text", "")
            if result_text:
                attempt_num = prev_attempt + cycle
                cp_path = save_checkpoint(task_id, attempt_num, result_text, role="developer")
                if cp_path:
                    log(f"  [Checkpoint] Saved ({len(result_text)} chars) -> {cp_path.name}", output)

            # Auto-continue: spawn fresh agent with checkpoint context
            if continuation_count < MAX_CONTINUATIONS:
                log(f"  [Auto-Continue] Spawning new developer agent to continue...", output)
                if result_text:
                    checkpoint_context = build_checkpoint_context(
                        result_text, prev_attempt + cycle)
                    compaction_history.append(checkpoint_context)
                continue  # skip tester, go to next dev-test cycle

            # All continuations exhausted
            log(f"  [Auto-Continue] All {MAX_CONTINUATIONS} continuations exhausted. "
                f"Marking blocked.", output)
            outcome = "developer_timeout" if is_timeout else "developer_max_turns"
            return dev_result, cycle, outcome

        # Check for agent failure
        if not dev_result["success"]:
            log(f"  [Cycle {cycle}] Developer agent failed.", output)
            return dev_result, cycle, "developer_failed"

        # Compaction: if Developer used many turns, capture summary
        if dev_result.get("num_turns", 0) >= DEV_COMPACTION_THRESHOLD:
            log(f"  [Cycle {cycle}] Developer used {dev_result['num_turns']} turns, "
                f"compacting context...", output)
            summary = build_compaction_summary("Developer", dev_result, cycle, task)
            compaction_history.append(summary)

        # Check if Developer marked task blocked
        status = _get_task_status(task["id"])
        if status == "blocked":
            log(f"  [Cycle {cycle}] Developer marked task as BLOCKED.", output)
            return dev_result, cycle, "developer_blocked"

        # Progress detection: check FILES_CHANGED marker + turn-based heuristic
        files_changed = parse_developer_output(dev_result.get("result_text", ""))
        dev_turns_used = dev_result.get("num_turns", 0)

        # Consider it progress if: FILES_CHANGED has items, OR dev used 3+ turns
        # (agents often do work but forget to output the marker)
        made_progress = bool(files_changed) or dev_turns_used >= 3

        if not made_progress:
            no_progress_count += 1
            log(f"  [Cycle {cycle}] No progress detected ({dev_turns_used} turns, no files marker) "
                f"({no_progress_count}/{NO_PROGRESS_LIMIT} consecutive).", output)
            if no_progress_count >= NO_PROGRESS_LIMIT:
                log(f"  [Cycle {cycle}] No progress for {NO_PROGRESS_LIMIT} cycles. "
                    f"Marking blocked.", output)
                return dev_result, cycle, "no_progress"
        else:
            no_progress_count = 0
            if files_changed:
                log(f"  [Cycle {cycle}] Developer changed {len(files_changed)} file(s): "
                    f"{', '.join(files_changed[:5])}", output)
            else:
                log(f"  [Cycle {cycle}] Developer used {dev_turns_used} turns "
                    f"(no FILES_CHANGED marker, but counting as progress).", output)

        # --- Tester Phase ---
        log(f"\n  [Cycle {cycle}] Running Tester agent...", output)

        tester_prompt = build_system_prompt(
            task, project_context, project_dir, role="tester",
        )
        tester_cmd = build_cli_command(
            tester_prompt, project_dir, tester_turns_limit, tester_model, role="tester",
        )

        tester_result = await run_agent(tester_cmd)
        total_duration += tester_result.get("duration", 0)
        if tester_result.get("cost"):
            total_cost += tester_result["cost"]

        # Check for timeout
        if any("timed out" in e for e in tester_result.get("errors", [])):
            log(f"  [Cycle {cycle}] Tester timed out.", output)
            return tester_result, cycle, "tester_timeout"

        # Compaction: if Tester used many turns, capture summary
        if tester_result.get("num_turns", 0) >= TESTER_COMPACTION_THRESHOLD:
            log(f"  [Cycle {cycle}] Tester used {tester_result['num_turns']} turns, "
                f"compacting context...", output)
            summary = build_compaction_summary("Tester", tester_result, cycle, task)
            compaction_history.append(summary)

        # Parse Tester output
        test_results = parse_tester_output(tester_result.get("result_text", ""))
        test_outcome = test_results["result"]

        log(f"  [Cycle {cycle}] Tester result: {test_outcome} "
            f"({test_results['tests_passed']}/{test_results['tests_run']} passed)", output)

        if test_outcome == "pass":
            log(f"  [Cycle {cycle}] All tests passed!", output)
            clear_checkpoints(task_id)  # success — no need for checkpoints
            tester_result["cost"] = total_cost
            tester_result["duration"] = total_duration
            return tester_result, cycle, "tests_passed"

        elif test_outcome == "no-tests":
            log(f"  [Cycle {cycle}] No tests found. Accepting Developer result.", output)
            clear_checkpoints(task_id)  # success — no need for checkpoints
            dev_result["cost"] = total_cost
            dev_result["duration"] = total_duration
            return dev_result, cycle, "no_tests"

        elif test_outcome == "blocked":
            log(f"  [Cycle {cycle}] Tester is blocked (missing dependency, build error, etc.).", output)
            return tester_result, cycle, "tester_blocked"

        else:
            # test_outcome == "fail" or unknown
            log(f"  [Cycle {cycle}] {test_results['tests_failed']} test(s) failed.", output)
            if test_results["failure_details"]:
                for detail in test_results["failure_details"][:5]:
                    safe_detail = detail.encode("ascii", errors="replace").decode("ascii")
                    log(f"    - {safe_detail}", output)

            # Feed failures to next Developer cycle
            failure_context = build_test_failure_context(test_results, cycle)
            compaction_history.append(failure_context)

    # All cycles exhausted
    log(f"\n  All {MAX_DEV_TEST_CYCLES} dev-test cycles exhausted. Marking blocked.", output)
    # Return the last tester result with accumulated totals
    tester_result["cost"] = total_cost
    tester_result["duration"] = total_duration
    return tester_result, MAX_DEV_TEST_CYCLES, "cycles_exhausted"


# --- Security Review (automatic post dev-test) ---


async def run_security_review(task, project_dir, project_context, args, output=None):
    """Run an automatic security review after dev-test succeeds.

    Uses the security-reviewer role with ClaudeStick tools.
    Only runs if security_review is enabled in dispatch config.
    """
    log(f"\n{'=' * 50}", output)
    log(f"  SECURITY REVIEW", output)
    log(f"{'=' * 50}", output)
    log(f"\n  Running security reviewer agent...", output)

    # Build security review prompt with explicit instructions to use all tools
    security_task = dict(task)  # copy
    security_task["description"] = (
        f"Security review of code written for: {task['title']}. "
        f"Review ALL files changed in the project directory. "
        f"YOU MUST use ALL ClaudeStick security tools: static-analysis, "
        f"audit-context-building, variant-analysis, differential-review, "
        f"fix-review, semgrep-rule-creator, and sharp-edges. "
        f"Check for OWASP Top 10 vulnerabilities, zero-day risks in dependencies, "
        f"and any security anti-patterns. "
        f"Write findings to a SECURITY-REVIEW.md file in the project directory. "
        f"Rate each finding: CRITICAL, HIGH, MEDIUM, LOW, INFO. "
        f"Original task description: {task['description']}"
    )

    sec_prompt = build_system_prompt(
        security_task, project_context, project_dir,
        role="security-reviewer",
    )
    sec_turns = get_role_turns("security-reviewer", args, task=task)
    sec_model = get_role_model("security-reviewer", args, task=task)
    sec_cmd = build_cli_command(
        sec_prompt, project_dir, sec_turns, sec_model, role="security-reviewer",
    )

    # Use security_review_timeout from dispatch config (default 15 min)
    dc = load_dispatch_config()
    sec_timeout = dc.get("security_review_timeout", 900)
    sec_result = await run_agent(sec_cmd, timeout=sec_timeout)

    if sec_result["success"]:
        log(f"  Security review completed in {sec_result.get('duration', 0):.1f}s", output)
        # Parse for critical findings
        result_text = sec_result.get("result_text", "")
        critical_count = result_text.lower().count("critical")
        high_count = result_text.lower().count("high")
        if critical_count > 0 or high_count > 0:
            log(f"  WARNING: Found {critical_count} CRITICAL and {high_count} HIGH severity findings", output)
        else:
            log(f"  No critical or high severity findings", output)
    else:
        log(f"  Security review agent failed.", output)
        for err in sec_result.get("errors", []):
            log(f"    Error: {err[:200]}", output)

    return sec_result


# --- Manager Mode: Planner & Evaluator (Phase 3) ---

def build_planner_prompt(goal, project_id, project_dir, project_context):
    """Build the system prompt for the Planner agent.

    The Planner gets the goal, project context, and codebase access.
    It does NOT get a task — it creates tasks.
    """
    common_path = PROMPTS_DIR / "_common.md"
    role_path = ROLE_PROMPTS["planner"]

    common_text = common_path.read_text(encoding="utf-8")
    role_text = role_path.read_text(encoding="utf-8")
    template = common_text + "\n\n---\n\n" + role_text

    # Replace project_id placeholder (planner doesn't have a task_id)
    prompt = template.replace("{project_id}", str(project_id))
    prompt = prompt.replace("{task_id}", "N/A")

    # Append goal and project context
    lines = [
        "## Goal",
        "",
        '<task-input type="goal" trust="user">',
        goal,
        "</task-input>",
        "",
        f"## Project Info",
        f"- **Project ID:** {project_id}",
        f"- **Working Directory:** {project_dir}",
        "",
    ]

    # Add project context
    session = project_context.get("last_session")
    if session:
        lines.append("## Recent Project Context")
        lines.append('<task-input type="session-context" trust="database">')
        lines.append(f"Last session ({session.get('session_date', 'unknown')}):")
        lines.append(session.get("summary", "No summary"))
        if session.get("next_steps"):
            lines.append(f"Next steps: {session['next_steps']}")
        lines.append("</task-input>")
        lines.append("")

    questions = project_context.get("open_questions", [])
    if questions:
        lines.append("## Open Questions (unresolved)")
        lines.append('<task-input type="open-questions" trust="database">')
        for q in questions:
            lines.append(f"- {q['question']}")
        lines.append("</task-input>")
        lines.append("")

    prompt = prompt + "\n\n---\n\n" + "\n".join(lines)
    return prompt


def build_evaluator_prompt(goal, project_id, project_dir, project_context,
                           completed_tasks, blocked_tasks):
    """Build the system prompt for the Evaluator agent.

    The Evaluator gets the original goal, completed/blocked task info,
    and codebase access to verify work.
    """
    common_path = PROMPTS_DIR / "_common.md"
    role_path = ROLE_PROMPTS["evaluator"]

    common_text = common_path.read_text(encoding="utf-8")
    role_text = role_path.read_text(encoding="utf-8")
    template = common_text + "\n\n---\n\n" + role_text

    prompt = template.replace("{project_id}", str(project_id))
    prompt = prompt.replace("{task_id}", "N/A")

    lines = [
        "## Original Goal",
        "",
        '<task-input type="goal" trust="user">',
        goal,
        "</task-input>",
        "",
        f"## Project Info",
        f"- **Project ID:** {project_id}",
        f"- **Working Directory:** {project_dir}",
        "",
    ]

    # Show completed tasks
    if completed_tasks:
        lines.append("## Completed Tasks")
        for t in completed_tasks:
            lines.append(f"- **#{t['id']}** {t['title']} — {t.get('description', 'No description')[:200]}")
        lines.append("")

    # Show blocked tasks
    if blocked_tasks:
        lines.append("## Blocked Tasks")
        for t in blocked_tasks:
            lines.append(f"- **#{t['id']}** {t['title']} — {t.get('description', 'No description')[:200]}")
        lines.append("")

    if not completed_tasks and not blocked_tasks:
        lines.append("## Task Results")
        lines.append("No tasks were completed or blocked. This is unexpected.")
        lines.append("")

    prompt = prompt + "\n\n---\n\n" + "\n".join(lines)
    return prompt


def parse_planner_output(result_text):
    """Extract TASKS_CREATED list from Planner agent output.

    Returns a list of integer task IDs, or empty list on failure.
    """
    if not result_text:
        return []

    for line in result_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("TASKS_CREATED:"):
            value = stripped.split(":", 1)[1].strip()
            if not value or value.lower() == "none":
                return []
            # Parse comma-separated IDs
            ids = []
            for part in value.split(","):
                part = part.strip()
                try:
                    ids.append(int(part))
                except ValueError:
                    continue
            return ids

    return []


def parse_evaluator_output(result_text):
    """Extract GOAL_STATUS, TASKS_CREATED, EVALUATION, BLOCKERS from Evaluator output.

    Returns a dict with parsed fields.
    """
    parsed = {
        "goal_status": "blocked",  # default to blocked if parsing fails
        "tasks_created": [],
        "evaluation": "",
        "blockers": "none",
    }

    if not result_text:
        return parsed

    for line in result_text.splitlines():
        stripped = line.strip()

        if stripped.startswith("GOAL_STATUS:"):
            status = stripped.split(":", 1)[1].strip().lower()
            if status in ("complete", "needs_more", "blocked"):
                parsed["goal_status"] = status

        elif stripped.startswith("TASKS_CREATED:"):
            value = stripped.split(":", 1)[1].strip()
            if value and value.lower() != "none":
                for part in value.split(","):
                    part = part.strip()
                    try:
                        parsed["tasks_created"].append(int(part))
                    except ValueError:
                        continue

        elif stripped.startswith("EVALUATION:"):
            parsed["evaluation"] = stripped.split(":", 1)[1].strip()

        elif stripped.startswith("BLOCKERS:"):
            parsed["blockers"] = stripped.split(":", 1)[1].strip()

    return parsed


async def run_planner_agent(goal, project_id, project_dir, project_context, args,
                            output=None):
    """Spawn the Planner agent to break a goal into tasks.

    Returns (result, task_ids) tuple.
    """
    log("\n  [Planner] Building prompt...", output)
    system_prompt = build_planner_prompt(goal, project_id, project_dir, project_context)

    cmd = [
        "claude",
        "-p",
        f"Break this goal into tasks. Project dir: {project_dir}",
        "--output-format", "json",
        "--model", args.model,
        "--max-turns", str(get_role_turns("planner", args)),
        "--no-session-persistence",
        "--append-system-prompt", system_prompt,
        "--mcp-config", str(MCP_CONFIG),
        "--add-dir", str(project_dir),
        "--permission-mode", "bypassPermissions",
    ]

    log(f"  [Planner] Spawning agent (prompt: {len(system_prompt)} chars)...", output)
    result = await run_agent(cmd)

    if not result["success"]:
        log(f"  [Planner] Agent failed: {result.get('errors', [])}", output)
        return result, []

    task_ids = parse_planner_output(result.get("result_text", ""))

    # Validate task count
    if len(task_ids) > MAX_TASKS_PER_PLAN:
        log(f"  [Planner] Created {len(task_ids)} tasks (max {MAX_TASKS_PER_PLAN}). "
            f"Using first {MAX_TASKS_PER_PLAN}.", output)
        task_ids = task_ids[:MAX_TASKS_PER_PLAN]

    if task_ids:
        log(f"  [Planner] Created {len(task_ids)} tasks: {task_ids}", output)
    else:
        log(f"  [Planner] No task IDs found in output.", output)

    return result, task_ids


async def run_evaluator_agent(goal, project_id, project_dir, project_context,
                               completed_tasks, blocked_tasks, args, output=None):
    """Spawn the Evaluator agent to assess goal completion.

    Returns (result, parsed_eval) tuple.
    """
    log("\n  [Evaluator] Building prompt...", output)
    system_prompt = build_evaluator_prompt(
        goal, project_id, project_dir, project_context,
        completed_tasks, blocked_tasks,
    )

    cmd = [
        "claude",
        "-p",
        f"Evaluate whether this goal is complete. Project dir: {project_dir}",
        "--output-format", "json",
        "--model", args.model,
        "--max-turns", str(get_role_turns("evaluator", args)),
        "--no-session-persistence",
        "--append-system-prompt", system_prompt,
        "--mcp-config", str(MCP_CONFIG),
        "--add-dir", str(project_dir),
        "--permission-mode", "bypassPermissions",
    ]

    log(f"  [Evaluator] Spawning agent (prompt: {len(system_prompt)} chars)...", output)
    result = await run_agent(cmd)

    if not result["success"]:
        log(f"  [Evaluator] Agent failed: {result.get('errors', [])}", output)
        return result, {"goal_status": "blocked", "tasks_created": [],
                        "evaluation": "Evaluator agent failed", "blockers": "Agent error"}

    parsed = parse_evaluator_output(result.get("result_text", ""))

    # Validate follow-up task count
    if len(parsed["tasks_created"]) > MAX_FOLLOWUP_TASKS:
        log(f"  [Evaluator] Created {len(parsed['tasks_created'])} follow-up tasks "
            f"(max {MAX_FOLLOWUP_TASKS}). Using first {MAX_FOLLOWUP_TASKS}.", output)
        parsed["tasks_created"] = parsed["tasks_created"][:MAX_FOLLOWUP_TASKS]

    log(f"  [Evaluator] Goal status: {parsed['goal_status']}", output)
    log(f"  [Evaluator] Evaluation: {parsed['evaluation'][:200]}", output)
    if parsed["tasks_created"]:
        log(f"  [Evaluator] Follow-up tasks: {parsed['tasks_created']}", output)

    return result, parsed


async def run_manager_loop(goal, project_id, project_dir, project_context, args,
                            output=None):
    """Run the full Manager loop: Plan -> Execute -> Evaluate -> Repeat.

    Returns (outcome, total_rounds, all_completed, all_blocked, total_cost, total_duration).
    """
    max_rounds = args.max_rounds
    all_completed = []
    all_blocked = []
    total_cost = 0.0
    total_duration = 0.0

    for round_num in range(1, max_rounds + 1):
        log(f"\n{'#' * 60}", output)
        log(f"  MANAGER ROUND {round_num}/{max_rounds}", output)
        log(f"{'#' * 60}", output)

        # --- Phase 1: Plan ---
        log(f"\n--- Phase 1: Planning ---", output)
        planner_result, task_ids = await run_planner_agent(
            goal, project_id, project_dir, project_context, args, output=output,
        )
        total_duration += planner_result.get("duration", 0)
        if planner_result.get("cost"):
            total_cost += planner_result["cost"]

        if not task_ids:
            log(f"\n  [Manager] Planner failed to create tasks. Aborting.", output)
            return "planner_failed", round_num, all_completed, all_blocked, total_cost, total_duration

        # Fetch the created tasks
        tasks = fetch_tasks_by_ids(task_ids)
        if not tasks:
            log(f"\n  [Manager] Could not fetch tasks {task_ids} from TheForge. Aborting.", output)
            return "planner_failed", round_num, all_completed, all_blocked, total_cost, total_duration

        # --- Phase 2: Execute each task via Dev+Tester loop ---
        log(f"\n--- Phase 2: Executing {len(tasks)} tasks ---", output)
        round_completed = []
        round_blocked = []

        for i, task in enumerate(tasks, 1):
            log(f"\n{'=' * 50}", output)
            log(f"  TASK {i}/{len(tasks)}: #{task['id']} - {task['title']}", output)
            log(f"{'=' * 50}", output)

            # Re-fetch task status (planner may have set it to todo)
            current_status = _get_task_status(task["id"])
            if current_status == "done":
                log(f"  Task already marked done. Skipping.", output)
                round_completed.append(task)
                continue

            result, cycles, outcome = await run_dev_test_loop(
                task, project_dir, project_context, args, output=output,
            )
            total_duration += result.get("duration", 0)
            if result.get("cost"):
                total_cost += result["cost"]

            # Orchestrator-side DB update (don't rely on agent)
            update_task_status(task["id"], outcome, output=output)

            if outcome in ("tests_passed", "no_tests"):
                round_completed.append(task)
                log(f"  Task #{task['id']}: COMPLETED ({outcome})", output)
            else:
                round_blocked.append(task)
                log(f"  Task #{task['id']}: BLOCKED ({outcome})", output)

        all_completed.extend(round_completed)
        all_blocked.extend(round_blocked)

        # --- Phase 3: Evaluate ---
        log(f"\n--- Phase 3: Evaluating ---", output)
        log(f"  Completed: {len(round_completed)}, Blocked: {len(round_blocked)}", output)

        eval_result, eval_parsed = await run_evaluator_agent(
            goal, project_id, project_dir, project_context,
            all_completed, all_blocked, args, output=output,
        )
        total_duration += eval_result.get("duration", 0)
        if eval_result.get("cost"):
            total_cost += eval_result["cost"]

        if eval_parsed["goal_status"] == "complete":
            log(f"\n  [Manager] Goal COMPLETE!", output)
            return "goal_complete", round_num, all_completed, all_blocked, total_cost, total_duration

        elif eval_parsed["goal_status"] == "blocked":
            log(f"\n  [Manager] Goal BLOCKED: {eval_parsed['blockers']}", output)
            return "goal_blocked", round_num, all_completed, all_blocked, total_cost, total_duration

        elif eval_parsed["goal_status"] == "needs_more":
            if round_num >= max_rounds:
                log(f"\n  [Manager] Needs more work but max rounds reached.", output)
                break
            log(f"\n  [Manager] Needs more work. Continuing to round {round_num + 1}...", output)
            if eval_parsed["tasks_created"]:
                continue

    return "rounds_exhausted", max_rounds, all_completed, all_blocked, total_cost, total_duration


def print_manager_summary(goal, outcome, rounds, completed, blocked, cost, duration,
                          output=None):
    """Print a formatted summary of the Manager mode run."""
    outcome_messages = {
        "goal_complete": "Goal achieved successfully",
        "goal_blocked": "Goal blocked — cannot proceed",
        "planner_failed": "Planner failed to create tasks",
        "rounds_exhausted": f"Max rounds ({rounds}) exhausted without completion",
    }

    is_success = outcome == "goal_complete"

    log("\n" + "#" * 60, output)
    log("FORGETEAM MANAGER MODE SUMMARY", output)
    log("#" * 60, output)

    log(f"\nGoal:      {goal[:100]}", output)
    log(f"Verdict:   {'SUCCESS' if is_success else 'INCOMPLETE'}", output)
    log(f"Outcome:   {outcome_messages.get(outcome, outcome)}", output)
    log(f"Rounds:    {rounds}", output)
    log(f"Tasks:     {len(completed)} completed, {len(blocked)} blocked", output)
    log(f"Duration:  {duration:.1f}s total", output)
    if cost > 0:
        log(f"Cost:      ${cost:.4f} total", output)

    if completed:
        log(f"\nCompleted Tasks:", output)
        for t in completed:
            log(f"  - #{t['id']} {t['title']}", output)

    if blocked:
        log(f"\nBlocked Tasks:", output)
        for t in blocked:
            log(f"  - #{t['id']} {t['title']}", output)

    log("\n" + "#" * 60, output)


# --- Verification ---

def verify_task_updated(task_id):
    """Check if the agent updated the task status in TheForge."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

        if not row:
            return False, f"Task {task_id} not found in database"

        status = row["status"]
        if status == "done":
            return True, f"Task {task_id} marked as DONE"
        elif status == "blocked":
            return True, f"Task {task_id} marked as BLOCKED (agent reported blocker)"
        elif status == "in_progress":
            return False, f"Task {task_id} still IN_PROGRESS (agent may not have finished)"
        else:
            return False, f"Task {task_id} status is '{status}' (expected done or blocked)"
    finally:
        conn.close()


# --- Output ---

def print_summary(task, result, verified, verify_msg):
    """Print a formatted summary of the agent run."""
    print("\n" + "=" * 60)
    print("FORGETEAM AGENT RUN SUMMARY")
    print("=" * 60)

    print(f"\nTask:      #{task['id']} - {task['title']}")
    print(f"Project:   {task.get('project_name', 'Unknown')}")
    print(f"Status:    {'SUCCESS' if result['success'] else 'FAILED'}")
    print(f"Turns:     {result['num_turns']}")
    print(f"Duration:  {result['duration']:.1f}s")

    if result["cost"] is not None:
        print(f"Cost:      ${result['cost']:.4f}")

    print(f"\nDB Verify: {'PASS' if verified else 'FAIL'} - {verify_msg}")

    if result["errors"]:
        print(f"\nErrors:")
        for err in result["errors"]:
            # Truncate long error messages
            if len(err) > 200:
                err = err[:200] + "..."
            print(f"  - {err}")

    # Show tail of agent output
    output = result.get("result_text", "")
    if output:
        print(f"\nAgent Output (last 500 chars):")
        print("-" * 40)
        tail = output[-500:] if len(output) > 500 else output
        # Handle Windows console encoding (cp1252 can't print emoji etc.)
        print(tail.encode("ascii", errors="replace").decode("ascii"))

    print("\n" + "=" * 60)


def print_dev_test_summary(task, result, cycles, outcome, verified, verify_msg):
    """Print a formatted summary of the dev-test loop run."""
    # Map outcome reasons to human-readable messages
    outcome_messages = {
        "tests_passed": "All tests passed",
        "no_tests": "No tests found, Developer result accepted",
        "developer_blocked": "Developer marked task as blocked",
        "developer_timeout": "Developer agent timed out",
        "developer_failed": "Developer agent failed",
        "tester_blocked": "Tester could not run (build error, missing deps)",
        "tester_timeout": "Tester agent timed out",
        "no_progress": f"No file changes for {NO_PROGRESS_LIMIT} consecutive cycles",
        "cycles_exhausted": f"All {MAX_DEV_TEST_CYCLES} fix-test cycles used without passing",
    }

    is_success = outcome in ("tests_passed", "no_tests")

    print("\n" + "=" * 60)
    print("FORGETEAM DEV-TEST LOOP SUMMARY")
    print("=" * 60)

    print(f"\nTask:      #{task['id']} - {task['title']}")
    print(f"Project:   {task.get('project_name', 'Unknown')}")
    print(f"Verdict:   {'SUCCESS' if is_success else 'BLOCKED'}")
    print(f"Cycles:    {cycles}/{MAX_DEV_TEST_CYCLES}")
    print(f"Outcome:   {outcome_messages.get(outcome, outcome)}")
    print(f"Duration:  {result.get('duration', 0):.1f}s total")

    if result.get("cost") is not None:
        print(f"Cost:      ${result['cost']:.4f} total")

    print(f"\nDB Verify: {'PASS' if verified else 'FAIL'} - {verify_msg}")

    if result.get("errors"):
        print(f"\nErrors:")
        for err in result["errors"]:
            if len(err) > 200:
                err = err[:200] + "..."
            print(f"  - {err}")

    # Show tail of last agent output
    output = result.get("result_text", "")
    if output:
        print(f"\nLast Agent Output (last 500 chars):")
        print("-" * 40)
        tail = output[-500:] if len(output) > 500 else output
        print(tail.encode("ascii", errors="replace").decode("ascii"))

    print("\n" + "=" * 60)


def resolve_project_dir(task):
    """Find the project directory for a task's project.

    Uses exact match only — no partial/substring matching to prevent
    path traversal via crafted project codenames (security finding #3).
    """
    codename = task.get("project_codename", "").lower().strip()
    project_name = task.get("project_name", "").lower().strip()

    # Exact match on codename first, then project name
    if codename and codename in PROJECT_DIRS:
        return PROJECT_DIRS[codename]
    if project_name and project_name in PROJECT_DIRS:
        return PROJECT_DIRS[project_name]

    return None


# --- Parallel Goals (Phase 4A) ---

def load_goals_file(filepath):
    """Parse and validate a goals JSON file.

    Returns (defaults_dict, goals_list) tuple.
    Exits with error on invalid input.
    """
    path = Path(filepath)
    if not path.exists():
        print(f"ERROR: Goals file not found: {filepath}")
        sys.exit(1)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in goals file: {e}")
        sys.exit(1)

    if "goals" not in data or not isinstance(data["goals"], list):
        print("ERROR: Goals file must contain a 'goals' array")
        sys.exit(1)

    if not data["goals"]:
        print("ERROR: Goals array is empty")
        sys.exit(1)

    # Extract defaults
    defaults = {
        "max_concurrent": data.get("max_concurrent", 4),
        "model": data.get("model", DEFAULT_MODEL),
        "max_turns": data.get("max_turns", DEFAULT_MAX_TURNS),
        "max_rounds": data.get("max_rounds", MAX_MANAGER_ROUNDS),
    }

    # Validate each goal
    for i, g in enumerate(data["goals"]):
        if "goal" not in g:
            print(f"ERROR: Goal #{i + 1} missing 'goal' field")
            sys.exit(1)
        if "project_id" not in g:
            print(f"ERROR: Goal #{i + 1} missing 'project_id' field")
            sys.exit(1)

    return defaults, data["goals"]


def validate_goals(goals):
    """Validate goals: check project_ids exist, dirs exist, no duplicates.

    Returns list of resolved goal dicts with project_dir and project_info added.
    Exits with error on validation failure.
    """
    # Check for duplicate project_ids
    project_ids = [g["project_id"] for g in goals]
    seen = set()
    for pid in project_ids:
        if pid in seen:
            print(f"ERROR: Duplicate project_id {pid} in goals file. "
                  f"Two goals cannot target the same project (they'd write to the same directory).")
            sys.exit(1)
        seen.add(pid)

    resolved = []
    for i, g in enumerate(goals):
        project_info = fetch_project_info(g["project_id"])
        if not project_info:
            print(f"ERROR: Goal #{i + 1}: Project {g['project_id']} not found in TheForge")
            sys.exit(1)

        codename = project_info.get("codename", "").lower().strip()
        pname = project_info.get("name", "").lower().strip()
        project_dir = PROJECT_DIRS.get(codename) or PROJECT_DIRS.get(pname)

        if not project_dir:
            print(f"ERROR: Goal #{i + 1}: No directory mapped for project "
                  f"'{project_info.get('name', 'Unknown')}'")
            sys.exit(1)

        if not Path(project_dir).exists():
            print(f"ERROR: Goal #{i + 1}: Directory does not exist: {project_dir}")
            sys.exit(1)

        resolved.append({
            **g,
            "project_dir": project_dir,
            "project_info": project_info,
        })

    return resolved


async def run_single_goal(goal_entry, semaphore, index, defaults, args):
    """Run a single Manager loop for one goal, respecting the semaphore.

    Returns a result dict with goal info and outcome.
    """
    goal_text = goal_entry["goal"]
    project_id = goal_entry["project_id"]
    project_dir = goal_entry["project_dir"]
    project_name = goal_entry["project_info"].get("name", "Unknown")

    # Per-goal overrides or defaults
    model = goal_entry.get("model", defaults["model"])
    max_turns = goal_entry.get("max_turns", defaults["max_turns"])
    max_rounds = goal_entry.get("max_rounds", defaults["max_rounds"])

    # Create a namespace that looks like args for the manager loop
    goal_args = argparse.Namespace(
        model=model,
        max_turns=max_turns,
        max_rounds=max_rounds,
    )

    output = []  # Buffer all output for this goal
    log(f"\n[Goal {index + 1}] {goal_text}", output)
    log(f"  Project: {project_name} (ID: {project_id})", output)
    log(f"  Directory: {project_dir}", output)
    log(f"  Model: {model}, Max turns: {max_turns}, Max rounds: {max_rounds}", output)

    async with semaphore:
        log(f"\n[Goal {index + 1}] Acquired slot, starting...", output)
        project_context = fetch_project_context(project_id)

        try:
            outcome, rounds, completed, blocked, cost, duration = await run_manager_loop(
                goal_text, project_id, project_dir, project_context,
                goal_args, output=output,
            )
        except Exception as e:
            log(f"\n[Goal {index + 1}] EXCEPTION: {e}", output)
            return {
                "index": index,
                "goal": goal_text,
                "project_name": project_name,
                "project_id": project_id,
                "outcome": "exception",
                "error": str(e),
                "rounds": 0,
                "completed": [],
                "blocked": [],
                "cost": 0.0,
                "duration": 0.0,
                "output": output,
            }

        print_manager_summary(
            goal_text, outcome, rounds, completed, blocked, cost, duration,
            output=output,
        )

    return {
        "index": index,
        "goal": goal_text,
        "project_name": project_name,
        "project_id": project_id,
        "outcome": outcome,
        "rounds": rounds,
        "completed": completed,
        "blocked": blocked,
        "cost": cost,
        "duration": duration,
        "output": output,
    }


async def run_parallel_goals(resolved_goals, defaults, args):
    """Run multiple Manager loops concurrently with a semaphore.

    Prints each goal's buffered output as it completes, then a combined summary.
    """
    max_concurrent = args.max_concurrent or defaults["max_concurrent"]
    semaphore = asyncio.Semaphore(max_concurrent)

    print(f"\nStarting {len(resolved_goals)} parallel goals "
          f"(max {max_concurrent} concurrent)...\n")

    # Launch all goals
    tasks = [
        run_single_goal(g, semaphore, i, defaults, args)
        for i, g in enumerate(resolved_goals)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Print each goal's buffered output
    for r in results:
        if isinstance(r, Exception):
            print(f"\n{'!' * 60}")
            print(f"  GOAL EXCEPTION: {r}")
            print(f"{'!' * 60}")
            continue

        print(f"\n{'=' * 60}")
        print(f"  OUTPUT: Goal {r['index'] + 1} — {r['project_name']}")
        print(f"{'=' * 60}")
        for line in r.get("output", []):
            print(line)

    # Print combined summary
    print_parallel_summary(results)


def print_parallel_summary(results):
    """Print a combined summary table for all parallel goals."""
    print(f"\n{'#' * 60}")
    print("FORGETEAM PARALLEL GOALS SUMMARY")
    print(f"{'#' * 60}")

    total_cost = 0.0
    total_duration = 0.0

    for r in results:
        if isinstance(r, Exception):
            print(f"\n  [?] EXCEPTION: {r}")
            continue

        is_success = r["outcome"] == "goal_complete"
        status = "OK" if is_success else r["outcome"].upper()
        n_completed = len(r.get("completed", []))
        n_blocked = len(r.get("blocked", []))
        cost = r.get("cost", 0.0)
        duration = r.get("duration", 0.0)

        total_cost += cost
        total_duration += duration

        print(f"\n  [{r['index'] + 1}] {r['project_name']} — {status}")
        print(f"      Goal: {r['goal'][:80]}")
        print(f"      Rounds: {r.get('rounds', '?')}, "
              f"Tasks: {n_completed} done / {n_blocked} blocked, "
              f"Duration: {duration:.1f}s")
        if cost > 0:
            print(f"      Cost: ${cost:.4f}")

    print(f"\n  TOTALS: {len(results)} goals, "
          f"Duration: {total_duration:.1f}s")
    if total_cost > 0:
        print(f"  Total Cost: ${total_cost:.4f}")

    print(f"\n{'#' * 60}")


# --- GitHub Repo Setup (Phase 4B) ---

def check_gh_installed():
    """Verify that gh CLI is installed and authenticated.

    Returns True if ready, prints error and returns False otherwise.
    """
    if not shutil.which("gh"):
        print("ERROR: GitHub CLI (gh) is not installed.")
        print("Install it from: https://cli.github.com/")
        return False

    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            print("ERROR: GitHub CLI is not authenticated.")
            print("Run: gh auth login")
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print("ERROR: Could not check gh auth status.")
        return False

    return True


def detect_project_language(project_dir):
    """Detect the primary language of a project by scanning for key files.

    Returns 'python', 'dotnet', 'node', or 'default'.
    """
    p = Path(project_dir)

    # Check for .NET
    if list(p.glob("*.csproj")) or list(p.glob("*.sln")) or list(p.glob("**/*.csproj")):
        return "dotnet"

    # Check for Node.js
    if (p / "package.json").exists():
        return "node"

    # Check for Python
    if (list(p.glob("*.py")) or (p / "pyproject.toml").exists()
            or (p / "setup.py").exists() or list(p.glob("**/*.py"))):
        return "python"

    return "default"


def _get_repo_env():
    """Build an environment dict with git and gh on the PATH."""
    env = os.environ.copy()
    extra_paths = []
    sep = ";" if platform.system() == "Windows" else ":"
    if platform.system() == "Windows":
        candidates = [
            r"C:\Program Files\Git\cmd",
            r"C:\Program Files\GitHub CLI",
        ]
    else:
        candidates = [
            "/usr/local/bin",
            str(Path.home() / ".local" / "bin"),
        ]
    for candidate in candidates:
        if os.path.isdir(candidate) and candidate not in env.get("PATH", ""):
            extra_paths.append(candidate)
    if extra_paths:
        env["PATH"] = sep.join(extra_paths) + sep + env.get("PATH", "")
    return env


def setup_single_repo(codename, project_dir, owner, dry_run=False):
    """Initialize git and create a GitHub private repo for a single project.

    Returns (success: bool, message: str).
    """
    p = Path(project_dir)
    repo_name = codename.lower().replace(" ", "-")

    # Skip if already fully set up (has .git AND a remote)
    git_dir = p / ".git"
    has_git = git_dir.exists()
    has_remote = False
    if has_git:
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, cwd=str(p), timeout=10,
                env=_get_repo_env(),
            )
            if result.returncode == 0 and result.stdout.strip():
                has_remote = True
                return True, f"Already set up (remote: {result.stdout.strip()})"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if dry_run:
        lang = detect_project_language(project_dir)
        return True, f"DRY RUN: Would init git, detect={lang}, create {owner}/{repo_name}"

    # Detect language and write .gitignore
    lang = detect_project_language(project_dir)
    gitignore_path = p / ".gitignore"
    if not gitignore_path.exists():
        template = GITIGNORE_TEMPLATES.get(lang, GITIGNORE_TEMPLATES["default"])
        gitignore_path.write_text(template + "\n", encoding="utf-8")
        print(f"    Created .gitignore ({lang})")
    else:
        print(f"    .gitignore already exists, keeping it")

    # git init (skip if already initialized from a prior run)
    if not has_git:
        result = subprocess.run(
            ["git", "init"],
            capture_output=True, text=True, cwd=str(p), timeout=30,
            env=_get_repo_env(),
        )
        if result.returncode != 0:
            return False, f"git init failed: {result.stderr.strip()}"
    else:
        print(f"    .git already exists, resuming setup")

    # git add . (warnings about CRLF go to stderr but are harmless)
    result = subprocess.run(
        ["git", "add", "."],
        capture_output=True, text=True, cwd=str(p), timeout=300,
        env=_get_repo_env(),
    )
    if result.returncode != 0:
        # Filter out CRLF warnings — only fail on actual errors
        real_errors = [
            line for line in result.stderr.strip().splitlines()
            if not line.startswith("warning:")
        ]
        if real_errors:
            return False, f"git add failed: {chr(10).join(real_errors)}"

    # git commit
    result = subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        capture_output=True, text=True, cwd=str(p), timeout=120,
        env=_get_repo_env(),
    )
    if result.returncode != 0:
        # Could be "nothing to commit" — that's OK
        if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
            print(f"    Nothing to commit (empty or already committed)")
        else:
            return False, f"git commit failed: {result.stderr.strip()}"

    # gh repo create
    result = subprocess.run(
        ["gh", "repo", "create", f"{owner}/{repo_name}",
         "--private", "--source=.", "--push"],
        capture_output=True, text=True, cwd=str(p), timeout=300,
        env=_get_repo_env(),
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "already exists" in stderr:
            # Repo exists, just add remote and push
            print(f"    Repo already exists on GitHub, adding remote...")
            subprocess.run(
                ["git", "remote", "add", "origin",
                 f"https://github.com/{owner}/{repo_name}.git"],
                capture_output=True, text=True, cwd=str(p), timeout=10,
                env=_get_repo_env(),
            )
            push_result = subprocess.run(
                ["git", "push", "-u", "origin", "main"],
                capture_output=True, text=True, cwd=str(p), timeout=120,
                env=_get_repo_env(),
            )
            if push_result.returncode != 0:
                # Try master branch
                subprocess.run(
                    ["git", "push", "-u", "origin", "master"],
                    capture_output=True, text=True, cwd=str(p), timeout=120,
                    env=_get_repo_env(),
                )
        else:
            return False, f"gh repo create failed: {stderr}"

    return True, f"Created https://github.com/{owner}/{repo_name}"


def setup_all_repos(args):
    """Initialize git + GitHub repos for all (or one) project.

    Uses --setup-repos for all, --setup-repos-project for a single project.
    """
    # Check prerequisites (skip for dry run)
    if not args.dry_run and not check_gh_installed():
        sys.exit(1)

    # synced storage warning
    print("\n" + "!" * 60)
    print("WARNING: Git repos in synced storage can experience corruption")
    print("from sync conflicts on the .git/index binary file.")
    print("")
    print("Recommendation: Avoid editing the same project on multiple")
    print("PCs simultaneously. The GitHub remote serves as your backup —")
    print("you can always re-clone if needed.")
    print("!" * 60)

    if not args.yes and not args.dry_run:
        response = input("\nContinue? (y/n): ").strip().lower()
        if response != "y":
            print("Aborted.")
            return

    # Determine which projects to set up
    if args.setup_repos_project:
        # Single project by ID
        project_info = fetch_project_info(args.setup_repos_project)
        if not project_info:
            print(f"ERROR: Project {args.setup_repos_project} not found in TheForge")
            sys.exit(1)

        codename = project_info.get("codename", "").lower().strip()
        pname = project_info.get("name", "").lower().strip()
        project_dir = PROJECT_DIRS.get(codename) or PROJECT_DIRS.get(pname)

        if not project_dir:
            print(f"ERROR: No directory mapped for project '{project_info.get('name')}'")
            sys.exit(1)

        targets = [(codename or pname, project_dir)]
    else:
        # All projects
        targets = list(PROJECT_DIRS.items())

    print(f"\nSetting up {len(targets)} project(s)...\n")

    results = []
    for codename, project_dir in targets:
        if not Path(project_dir).exists():
            print(f"  [{codename}] SKIP — directory does not exist: {project_dir}")
            results.append((codename, False, "Directory does not exist"))
            continue

        print(f"  [{codename}] {project_dir}")
        success, msg = setup_single_repo(codename, project_dir, GITHUB_OWNER, args.dry_run)
        status = "OK" if success else "FAIL"
        print(f"    -> {status}: {msg}")
        results.append((codename, success, msg))

    # Summary
    ok = sum(1 for _, s, _ in results if s)
    fail = len(results) - ok
    print(f"\nDone: {ok} succeeded, {fail} failed out of {len(results)} projects.")


# --- Auto-Run: DB Scanning & Scoring (Phase 5) ---

def scan_pending_work():
    """Query DB for all projects with todo tasks, grouped by priority.

    Returns a list of dicts:
    [
        {
            "project_id": 21,
            "project_name": "ForgeTeam",
            "codename": "forgeteam",
            "status": "active",
            "tasks": [<task dicts sorted by priority>],
            "counts": {"critical": 0, "high": 2, "medium": 1, "low": 0},
            "total_todo": 3,
        },
        ...
    ]
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT t.id, t.title, t.description, t.priority, t.project_id,
                   p.name as project_name,
                   COALESCE(p.codename, LOWER(REPLACE(p.name, ' ', ''))) as codename,
                   p.status as project_status
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.status = 'todo'
            ORDER BY t.project_id, t.created_at ASC
            """,
        ).fetchall()

        # Group by project
        projects = {}
        for row in rows:
            row = dict(row)
            pid = row["project_id"]
            if pid not in projects:
                projects[pid] = {
                    "project_id": pid,
                    "project_name": row["project_name"],
                    "codename": row["codename"],
                    "status": (row.get("project_status") or "unknown").lower(),
                    "tasks": [],
                    "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
                    "total_todo": 0,
                }
            projects[pid]["tasks"].append(row)
            projects[pid]["total_todo"] += 1
            priority = str(row.get("priority", "low")).lower()
            if priority in projects[pid]["counts"]:
                projects[pid]["counts"][priority] += 1

        # Sort tasks within each project by priority descending
        for proj in projects.values():
            proj["tasks"].sort(
                key=lambda t: PRIORITY_ORDER.get(
                    str(t.get("priority", "low")).lower(), 0
                ),
                reverse=True,
            )

        return list(projects.values())
    finally:
        conn.close()


def score_project(summary, config):
    """Score a project for dispatch priority.

    score = (critical*10) + (high*5) + (medium*2) + (low*1)
           + 3 if project status is 'active'
           + priority_boost from config
    """
    counts = summary["counts"]
    score = (
        counts.get("critical", 0) * 10
        + counts.get("high", 0) * 5
        + counts.get("medium", 0) * 2
        + counts.get("low", 0) * 1
    )

    if summary.get("status") == "active":
        score += 3

    # Apply manual boost from config
    codename = summary.get("codename", "").lower()
    boost = config.get("priority_boost", {})
    if codename in boost:
        score += boost[codename]
    # Also check by project_id string
    pid_str = str(summary.get("project_id", ""))
    if pid_str in boost:
        score += boost[pid_str]

    summary["score"] = score
    return score


# --- Auto-Run: Config Loading & Filters (Phase 5) ---

DEFAULT_DISPATCH_CONFIG = {
    "max_concurrent": 4,
    "model": "sonnet",
    "max_turns": 25,
    "max_tasks_per_project": 5,
    "skip_projects": [],
    "priority_boost": {},
    "only_projects": [],
    # Per-role model overrides (optional in config file)
    # "model_developer": "sonnet",
    # "model_tester": "haiku",
    # "model_security_reviewer": "sonnet",
    # Per-complexity model overrides (optional in config file)
    # "model_simple": "haiku",
    # "model_complex": "sonnet",
    # "model_epic": "opus",
}


def load_dispatch_config(filepath):
    """Load dispatch_config.json preferences.

    Returns a config dict with defaults for any missing keys.
    Falls back to defaults entirely if file not found.
    """
    config = dict(DEFAULT_DISPATCH_CONFIG)

    if filepath is None:
        # Try default location
        filepath = Path(__file__).parent / "dispatch_config.json"
    else:
        filepath = Path(filepath)

    if not filepath.exists():
        return config

    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: Could not load dispatch config '{filepath}': {e}")
        print("  Using defaults.")
        return config

    # Merge loaded values over defaults
    for key in DEFAULT_DISPATCH_CONFIG:
        if key in data:
            config[key] = data[key]

    # Also merge any extra keys not in defaults (model_developer, model_epic, etc.)
    for key in data:
        if key not in config:
            config[key] = data[key]

    return config


def apply_dispatch_filters(work, config, args):
    """Apply skip_projects, only_projects, and --only-project filters.

    Returns filtered list of project summaries.
    """
    filtered = list(work)

    # --only-project CLI args take highest priority
    cli_only = getattr(args, "only_project", None) or []
    if cli_only:
        cli_only_set = set(cli_only)
        filtered = [p for p in filtered if p["project_id"] in cli_only_set]
        return filtered

    # Config-level only_projects (whitelist mode)
    config_only = config.get("only_projects", [])
    if config_only:
        only_set = set()
        for item in config_only:
            if isinstance(item, int):
                only_set.add(item)
            elif isinstance(item, str):
                # Match by codename
                for p in filtered:
                    if p.get("codename", "").lower() == item.lower():
                        only_set.add(p["project_id"])
        filtered = [p for p in filtered if p["project_id"] in only_set]
        return filtered

    # Config-level skip_projects
    skip_list = config.get("skip_projects", [])
    if skip_list:
        skip_set = set()
        for item in skip_list:
            if isinstance(item, int):
                skip_set.add(item)
            elif isinstance(item, str):
                for p in filtered:
                    if p.get("codename", "").lower() == item.lower():
                        skip_set.add(p["project_id"])
        filtered = [p for p in filtered if p["project_id"] not in skip_set]

    return filtered


# --- Auto-Run: Per-Project Task Runner (Phase 5) ---

async def run_project_tasks(project_summary, config, args, output=None):
    """Run Dev+Test loops on todo tasks for one project, in priority order.

    Returns a dict with results per task.
    """
    project_id = project_summary["project_id"]
    codename = project_summary.get("codename", "unknown")
    tasks = project_summary["tasks"]

    # Apply max_tasks_per_project cap
    max_tasks = getattr(args, "max_tasks_per_project", None)
    if max_tasks is None:
        max_tasks = config.get("max_tasks_per_project", 5)
    if len(tasks) > max_tasks:
        log(f"  [{codename}] Capping to {max_tasks} tasks (of {len(tasks)} todo)", output)
        tasks = tasks[:max_tasks]

    # Resolve project directory
    codename_lower = codename.lower().strip()
    project_dir = PROJECT_DIRS.get(codename_lower)
    if not project_dir:
        log(f"  [{codename}] ERROR: No directory mapped. Skipping.", output)
        return {
            "project_id": project_id,
            "codename": codename,
            "tasks_attempted": 0,
            "tasks_completed": [],
            "tasks_blocked": [],
            "tasks_skipped": len(tasks),
            "error": "No directory mapped",
            "total_cost": 0.0,
            "total_duration": 0.0,
        }

    if not Path(project_dir).exists():
        log(f"  [{codename}] ERROR: Directory does not exist: {project_dir}. Skipping.", output)
        return {
            "project_id": project_id,
            "codename": codename,
            "tasks_attempted": 0,
            "tasks_completed": [],
            "tasks_blocked": [],
            "tasks_skipped": len(tasks),
            "error": "Directory does not exist",
            "total_cost": 0.0,
            "total_duration": 0.0,
        }

    project_context = fetch_project_context(project_id)

    completed = []
    blocked = []
    total_cost = 0.0
    total_duration = 0.0

    # Build args namespace for dev-test loop
    task_args = argparse.Namespace(
        model=config.get("model", args.model),
        max_turns=config.get("max_turns", args.max_turns),
        dispatch_config=config,  # pass config so get_role_turns can read per-role limits
    )

    for i, task_row in enumerate(tasks, 1):
        task_id = task_row["id"]
        log(f"\n  [{codename}] Task {i}/{len(tasks)}: #{task_id} - {task_row['title']}", output)

        # Re-fetch task to get full data with project info
        task = fetch_task(task_id)
        if not task:
            log(f"  [{codename}] Task #{task_id} not found in DB. Skipping.", output)
            continue

        # Check if still todo
        if task.get("status") != "todo":
            log(f"  [{codename}] Task #{task_id} status is '{task.get('status')}'. Skipping.", output)
            continue

        result, cycles, outcome = await run_dev_test_loop(
            task, project_dir, project_context, task_args, output=output,
        )
        total_duration += result.get("duration", 0)
        if result.get("cost"):
            total_cost += result["cost"]

        # Orchestrator-side DB update (don't rely on agent)
        update_task_status(task_id, outcome, output=output)

        if outcome in ("tests_passed", "no_tests"):
            completed.append(task)
            log(f"  [{codename}] Task #{task_id}: COMPLETED ({outcome})", output)
        else:
            blocked.append(task)
            log(f"  [{codename}] Task #{task_id}: BLOCKED ({outcome})", output)

    return {
        "project_id": project_id,
        "codename": codename,
        "tasks_attempted": len(tasks),
        "tasks_completed": completed,
        "tasks_blocked": blocked,
        "tasks_skipped": 0,
        "error": None,
        "total_cost": total_cost,
        "total_duration": total_duration,
    }


async def run_project_dispatch(project_summary, semaphore, config, args):
    """Wrapper for concurrent execution of one project's tasks.

    Acquires semaphore slot, runs tasks, returns result with buffered output.
    """
    codename = project_summary.get("codename", "unknown")
    output = []

    log(f"\n[{codename}] Queued ({project_summary['total_todo']} todo tasks, "
        f"score: {project_summary.get('score', '?')})", output)

    async with semaphore:
        log(f"[{codename}] Acquired slot, starting...", output)

        try:
            result = await run_project_tasks(
                project_summary, config, args, output=output,
            )
        except Exception as e:
            log(f"[{codename}] EXCEPTION: {e}", output)
            result = {
                "project_id": project_summary["project_id"],
                "codename": codename,
                "tasks_attempted": 0,
                "tasks_completed": [],
                "tasks_blocked": [],
                "tasks_skipped": project_summary["total_todo"],
                "error": str(e),
                "total_cost": 0.0,
                "total_duration": 0.0,
            }

    result["output"] = output
    return result


async def run_auto_dispatch(scored, config, args):
    """Run all project dispatches concurrently with semaphore.

    Prints each project's buffered output as it completes, then summary.
    """
    max_concurrent = config.get("max_concurrent", 4)
    semaphore = asyncio.Semaphore(max_concurrent)

    print(f"\nDispatching {len(scored)} projects "
          f"(max {max_concurrent} concurrent)...\n")

    coros = [
        run_project_dispatch(proj, semaphore, config, args)
        for proj in scored
    ]

    results = await asyncio.gather(*coros, return_exceptions=True)

    # Print each project's buffered output
    for r in results:
        if isinstance(r, Exception):
            print(f"\n{'!' * 60}")
            print(f"  PROJECT EXCEPTION: {r}")
            print(f"{'!' * 60}")
            continue

        print(f"\n{'=' * 60}")
        print(f"  OUTPUT: {r.get('codename', '?')}")
        print(f"{'=' * 60}")
        for line in r.get("output", []):
            print(line)

    # Print combined summary
    print_dispatch_summary(results)


# --- Auto-Run: Output Formatting (Phase 5) ---

def print_dispatch_plan(scored, config):
    """Preview: which projects will run, scores, task counts."""
    max_tasks = config.get("max_tasks_per_project", 5)
    max_concurrent = config.get("max_concurrent", 4)

    print(f"\n{'#' * 60}")
    print("AUTO-RUN DISPATCH PLAN")
    print(f"{'#' * 60}")
    print(f"\n  Max concurrent: {max_concurrent}")
    print(f"  Max tasks/project: {max_tasks}")
    print(f"  Model: {config.get('model', DEFAULT_MODEL)}")
    print(f"  Max turns: {config.get('max_turns', DEFAULT_MAX_TURNS)}")
    print()

    total_tasks = 0
    for i, proj in enumerate(scored, 1):
        counts = proj["counts"]
        capped = min(proj["total_todo"], max_tasks)
        total_tasks += capped

        codename_lower = proj.get("codename", "").lower().strip()
        has_dir = codename_lower in PROJECT_DIRS and Path(PROJECT_DIRS.get(codename_lower, "")).exists()

        print(f"  [{i}] {proj['project_name']} (ID: {proj['project_id']}, "
              f"codename: {proj.get('codename', '?')})")
        print(f"      Score: {proj.get('score', '?')} | Status: {proj.get('status', '?')}")
        print(f"      Tasks: {capped}/{proj['total_todo']} "
              f"(C:{counts['critical']} H:{counts['high']} "
              f"M:{counts['medium']} L:{counts['low']})")
        print(f"      Dir: {'OK' if has_dir else 'MISSING'}")
        print()

    print(f"  TOTAL: {len(scored)} projects, {total_tasks} tasks to attempt")
    print(f"\n{'#' * 60}")


def print_dispatch_summary(results):
    """Final report: tasks completed/blocked per project."""
    print(f"\n{'#' * 60}")
    print("AUTO-RUN DISPATCH SUMMARY")
    print(f"{'#' * 60}")

    total_completed = 0
    total_blocked = 0
    total_cost = 0.0
    total_duration = 0.0

    for r in results:
        if isinstance(r, Exception):
            print(f"\n  [?] EXCEPTION: {r}")
            continue

        n_completed = len(r.get("tasks_completed", []))
        n_blocked = len(r.get("tasks_blocked", []))
        total_completed += n_completed
        total_blocked += n_blocked

        cost = r.get("total_cost", 0.0)
        duration = r.get("total_duration", 0.0)
        total_cost += cost
        total_duration += duration

        codename = r.get("codename", "?")
        error = r.get("error")

        if error:
            print(f"\n  [{codename}] ERROR: {error}")
        else:
            status = "ALL DONE" if n_blocked == 0 and n_completed > 0 else "PARTIAL"
            if n_completed == 0 and n_blocked == 0:
                status = "NO TASKS"
            print(f"\n  [{codename}] {status}")
            print(f"      Tasks: {n_completed} completed, {n_blocked} blocked "
                  f"(of {r.get('tasks_attempted', '?')} attempted)")
            print(f"      Duration: {duration:.1f}s")
            if cost > 0:
                print(f"      Cost: ${cost:.4f}")

            # List completed task titles
            for t in r.get("tasks_completed", []):
                print(f"        + #{t['id']} {t['title']}")
            for t in r.get("tasks_blocked", []):
                print(f"        x #{t['id']} {t['title']}")

    print(f"\n  {'=' * 40}")
    print(f"  TOTALS: {total_completed} completed, {total_blocked} blocked")
    print(f"  Duration: {total_duration:.1f}s")
    if total_cost > 0:
        print(f"  Cost: ${total_cost:.4f}")
    print(f"\n{'#' * 60}")


def parse_task_ids(task_str):
    """Parse comma-separated IDs or ranges into a list of ints.

    Examples: "109,110,111" -> [109, 110, 111]
              "109-114" -> [109, 110, 111, 112, 113, 114]
              "109,112-114" -> [109, 112, 113, 114]
    """
    ids = []
    for part in task_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            ids.extend(range(int(start), int(end) + 1))
        else:
            ids.append(int(part))
    return ids


async def run_parallel_tasks(task_ids, args):
    """Run multiple tasks concurrently with dev-test loops.

    All tasks must belong to the same project (for safety).
    """
    # Fetch all tasks
    tasks = fetch_tasks_by_ids(task_ids)
    if not tasks:
        print("ERROR: No tasks found for given IDs.")
        return

    # Verify all tasks are from the same project
    project_ids = set(t.get("project_id") for t in tasks)
    if len(project_ids) > 1:
        print(f"ERROR: --tasks requires all tasks from the same project. "
              f"Found project IDs: {project_ids}")
        return

    project_id = tasks[0].get("project_id")
    project_dir = resolve_project_dir(tasks[0])
    if not project_dir:
        print(f"ERROR: Could not resolve project directory.")
        return
    if not Path(project_dir).exists():
        print(f"ERROR: Project directory does not exist: {project_dir}")
        return

    project_context = fetch_project_context(project_id)
    max_concurrent = getattr(args, "max_concurrent", None) or 4
    semaphore = asyncio.Semaphore(max_concurrent)

    print(f"\nParallel task execution: {len(tasks)} tasks, max {max_concurrent} concurrent")
    for t in tasks:
        print(f"  - #{t['id']}: {t['title']}")

    if not args.yes:
        response = input("\nProceed? (y/n): ").strip().lower()
        if response != "y":
            print("Aborted.")
            return

    async def run_one_task(task):
        output = []
        async with semaphore:
            log(f"\n[Task #{task['id']}] Starting: {task['title']}", output)
            result, cycles, outcome = await run_dev_test_loop(
                task, project_dir, project_context, args, output=output,
            )
            update_task_status(task["id"], outcome, output=output)
            log(f"[Task #{task['id']}] Done: {outcome} ({cycles} cycles)", output)
            return {
                "task": task,
                "result": result,
                "cycles": cycles,
                "outcome": outcome,
                "output": output,
            }

    results = await asyncio.gather(
        *[run_one_task(t) for t in tasks],
        return_exceptions=True,
    )

    # Print results
    print(f"\n{'#' * 60}")
    print("PARALLEL TASKS SUMMARY")
    print(f"{'#' * 60}")

    completed = []
    blocked = []
    total_cost = 0.0
    total_duration = 0.0

    for r in results:
        if isinstance(r, Exception):
            print(f"\n  EXCEPTION: {r}")
            continue

        task = r["task"]
        outcome = r["outcome"]
        result = r["result"]

        # Print buffered output
        for line in r.get("output", []):
            print(line)

        cost = result.get("cost", 0) or 0
        duration = result.get("duration", 0)
        total_cost += cost
        total_duration += duration

        if outcome in ("tests_passed", "no_tests"):
            completed.append(task)
            print(f"\n  #{task['id']}: COMPLETED ({outcome}, {r['cycles']} cycles, {duration:.0f}s)")
        else:
            blocked.append(task)
            print(f"\n  #{task['id']}: BLOCKED ({outcome}, {r['cycles']} cycles, {duration:.0f}s)")

    print(f"\nTotal: {len(completed)} completed, {len(blocked)} blocked")
    print(f"Duration: {total_duration:.0f}s total")
    if total_cost > 0:
        print(f"Cost: ${total_cost:.4f}")
    print(f"{'#' * 60}")


# --- Main ---

async def async_main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="ForgeTeam: Run AI agents on TheForge tasks"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--task", type=int, help="Task ID to work on")
    group.add_argument("--tasks", type=str, metavar="IDS",
                        help="Comma-separated task IDs or range (e.g. 109,110,111 or 109-114) for parallel execution")
    group.add_argument("--project", type=int, help="Project ID (auto-pick next todo task)")
    group.add_argument("--goal", type=str, help="High-level goal for Manager mode")
    group.add_argument("--parallel-goals", type=str, metavar="FILE",
                        help="Path to goals JSON file for parallel execution")
    group.add_argument("--setup-repos", action="store_true",
                        help="Init git + GitHub private repo for ALL projects")
    group.add_argument("--setup-repos-project", type=int, metavar="ID",
                        help="Init git + GitHub private repo for a single project")
    group.add_argument("--auto-run", action="store_true",
                        help="Auto-scan projects and dispatch work by priority")
    group.add_argument("--add-project", type=str, metavar="NAME",
                        help="Register a new project in Itzamna DB and config")

    parser.add_argument("--project-dir", type=str, metavar="PATH",
                        help="Project directory (used with --add-project)")
    parser.add_argument("--goal-project", type=int, help="Project ID (required with --goal)")
    parser.add_argument("--dispatch-config", type=str, metavar="FILE", default=None,
                        help="Path to dispatch config JSON (default: dispatch_config.json)")
    parser.add_argument("--max-tasks-per-project", type=int, default=None, metavar="N",
                        help="Cap tasks attempted per project per run")
    parser.add_argument("--only-project", type=int, action="append", default=None,
                        metavar="ID", help="Only run this project (repeatable)")
    parser.add_argument("--max-rounds", type=int, default=MAX_MANAGER_ROUNDS,
                        help=f"Max manager rounds (default: {MAX_MANAGER_ROUNDS})")
    parser.add_argument("--max-concurrent", type=int, default=None,
                        help="Override max concurrent goals (default: from goals file or 4)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model to use (default: {DEFAULT_MODEL})")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS, help=f"Max agent turns (default: {DEFAULT_MAX_TURNS})")
    parser.add_argument("--role", default="developer", choices=["developer", "security-reviewer", "tester"],
                        help="Agent role for single-agent mode (default: developer)")
    parser.add_argument("--retries", type=int, default=DEFAULT_MAX_RETRIES, help=f"Max retry attempts (default: {DEFAULT_MAX_RETRIES})")
    parser.add_argument("--dev-test", action="store_true", help="Enable Dev+Tester iteration loop mode")
    parser.add_argument("--security-review", action="store_true", default=None,
                        help="Run security review after dev-test passes (default: from dispatch config)")
    parser.add_argument("--dry-run", action="store_true", help="Print command without executing")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    args = parser.parse_args()

    # Auto-detect non-TTY (nohup, SSH pipe, etc.) and default to --yes
    if not args.yes and not sys.stdin.isatty():
        args.yes = True
        print("Non-interactive mode detected (stdin is not a TTY). Auto-enabling --yes.")

    # Validate --goal requires --goal-project
    if args.goal and not args.goal_project:
        parser.error("--goal requires --goal-project <project_id>")

    # Warn if --dev-test combined with --role
    if args.dev_test and args.role != "developer":
        print(f"WARNING: --dev-test mode ignores --role ('{args.role}'). "
              f"Loop uses Developer + Tester automatically.")

    # Load dispatch config globally so model tiering and adaptive turns work in all modes
    args.dispatch_config = load_dispatch_config(args.dispatch_config)

    # --- Add project mode ---
    if args.add_project:
        if not args.project_dir:
            parser.error("--add-project requires --project-dir <path>")
        _handle_add_project(args.add_project, args.project_dir)
        return

    # --- Setup repos mode (Phase 4B) ---
    if args.setup_repos or args.setup_repos_project:
        setup_all_repos(args)
        return

    # --- Auto-run mode (Phase 5) ---
    if args.auto_run:
        dispatch_config = args.dispatch_config

        # CLI overrides
        if args.max_concurrent is not None:
            dispatch_config["max_concurrent"] = args.max_concurrent
        if args.max_tasks_per_project is not None:
            dispatch_config["max_tasks_per_project"] = args.max_tasks_per_project

        # Scan DB for pending work
        print("Scanning TheForge for pending work...")
        work = scan_pending_work()
        if not work:
            print("No projects with todo tasks found.")
            return

        # Apply filters
        work = apply_dispatch_filters(work, dispatch_config, args)
        if not work:
            print("No projects match filters (check --only-project, skip_projects, only_projects).")
            return

        # Score and sort
        for proj in work:
            score_project(proj, dispatch_config)
        work.sort(key=lambda p: p.get("score", 0), reverse=True)

        # Dry run: show plan and exit
        if args.dry_run:
            print(f"\n--- DRY RUN (Auto-Run) ---")
            print_dispatch_plan(work, dispatch_config)
            print(f"\n--- END DRY RUN ---")
            return

        # Show plan and confirm
        print_dispatch_plan(work, dispatch_config)

        if not args.yes:
            response = input("\nProceed with dispatch? (y/n): ").strip().lower()
            if response != "y":
                print("Aborted.")
                return

        # Dispatch
        await run_auto_dispatch(work, dispatch_config, args)
        return

    # --- Parallel goals mode (Phase 4A) ---
    if args.parallel_goals:
        defaults, goals = load_goals_file(args.parallel_goals)
        resolved_goals = validate_goals(goals)

        # Apply per-goal defaults from file, allow CLI overrides
        if args.max_concurrent is not None:
            defaults["max_concurrent"] = args.max_concurrent

        if args.dry_run:
            print(f"\n--- DRY RUN (Parallel Goals) ---")
            print(f"Goals file: {args.parallel_goals}")
            print(f"Goals: {len(resolved_goals)}")
            print(f"Max concurrent: {defaults['max_concurrent']}")
            print(f"Default model: {defaults['model']}")
            print(f"Default max turns: {defaults['max_turns']}")
            print(f"Default max rounds: {defaults['max_rounds']}")
            print()
            for i, g in enumerate(resolved_goals):
                model = g.get("model", defaults["model"])
                print(f"  [{i + 1}] Project: {g['project_info']['name']} (ID: {g['project_id']})")
                print(f"      Goal: {g['goal'][:80]}")
                print(f"      Dir: {g['project_dir']}")
                print(f"      Model: {model}")
                planner_prompt = build_planner_prompt(
                    g["goal"], g["project_id"], g["project_dir"],
                    fetch_project_context(g["project_id"]),
                )
                print(f"      Planner prompt: {len(planner_prompt)} chars")
                print()
            print(f"--- END DRY RUN ---")
            return

        # Confirm
        if not args.yes:
            print(f"\nAbout to run {len(resolved_goals)} goals "
                  f"(max {defaults['max_concurrent']} concurrent).")
            for i, g in enumerate(resolved_goals):
                print(f"  [{i + 1}] {g['project_info']['name']}: {g['goal'][:60]}")
            response = input("\nProceed? (y/n): ").strip().lower()
            if response != "y":
                print("Aborted.")
                return

        await run_parallel_goals(resolved_goals, defaults, args)
        return

    # --- Manager mode (Phase 3) ---
    if args.goal:
        project_info = fetch_project_info(args.goal_project)
        if not project_info:
            print(f"ERROR: Project {args.goal_project} not found in TheForge")
            sys.exit(1)

        # Resolve project directory from project info
        codename = project_info.get("codename", "").lower().strip()
        project_name = project_info.get("name", "").lower().strip()
        project_dir = PROJECT_DIRS.get(codename) or PROJECT_DIRS.get(project_name)

        if not project_dir:
            print(f"ERROR: Could not find project directory for '{project_info.get('name', 'Unknown')}'")
            print("Known projects:", ", ".join(sorted(PROJECT_DIRS.keys())))
            sys.exit(1)

        if not Path(project_dir).exists():
            print(f"ERROR: Project directory does not exist: {project_dir}")
            sys.exit(1)

        project_context = fetch_project_context(args.goal_project)

        # Show goal info
        print(f"\nGoal: {args.goal}")
        print(f"Project: {project_info.get('name', 'Unknown')} (ID: {args.goal_project})")
        print(f"Directory: {project_dir}")
        print(f"Model: {args.model}")
        print(f"Max turns/agent: {args.max_turns}")
        print(f"Max rounds: {args.max_rounds}")

        if args.dry_run:
            # Show what the planner prompt would look like
            planner_prompt = build_planner_prompt(
                args.goal, args.goal_project, project_dir, project_context,
            )
            print(f"\n--- DRY RUN (Manager Mode) ---")
            print(f"Planner prompt: {len(planner_prompt)} chars")
            print(f"\nManager loop would run up to {args.max_rounds} rounds.")
            print(f"Each round: Planner -> Dev+Test loop per task -> Evaluator")
            print(f"\n--- END DRY RUN ---")
            return

        # Confirm before running
        if not args.yes:
            response = input("\nProceed? (y/n): ").strip().lower()
            if response != "y":
                print("Aborted.")
                return

        # Run the manager loop
        print(f"\nStarting Manager mode (max {args.max_rounds} rounds)...")
        outcome, rounds, completed, blocked, cost, duration = await run_manager_loop(
            args.goal, args.goal_project, project_dir, project_context, args,
        )

        # Print manager summary
        print_manager_summary(args.goal, outcome, rounds, completed, blocked, cost, duration)
        return

    # --- Parallel tasks mode ---
    if args.tasks:
        task_ids = parse_task_ids(args.tasks)
        if not task_ids:
            print("ERROR: Could not parse task IDs from --tasks argument.")
            sys.exit(1)

        if args.dry_run:
            tasks = fetch_tasks_by_ids(task_ids)
            print(f"\n--- DRY RUN (Parallel Tasks) ---")
            print(f"Tasks: {len(tasks)}")
            for t in tasks:
                print(f"  - #{t['id']}: {t['title']} ({t.get('project_name', '?')})")
            print(f"\n--- END DRY RUN ---")
            return

        await run_parallel_tasks(task_ids, args)
        return

    # --- Task/Project mode (Phase 1 & 2) ---

    # Fetch task
    if args.task:
        task = fetch_task(args.task)
        if not task:
            print(f"ERROR: Task {args.task} not found in TheForge")
            sys.exit(1)
    else:
        task = fetch_next_todo(args.project)
        if not task:
            print(f"No todo tasks found for project {args.project}")
            sys.exit(0)

    # Resolve project directory
    project_dir = resolve_project_dir(task)
    if not project_dir:
        print(f"ERROR: Could not find project directory for '{task.get('project_name', 'Unknown')}'")
        print("Known projects:", ", ".join(sorted(PROJECT_DIRS.keys())))
        sys.exit(1)

    # Verify project directory exists
    if not Path(project_dir).exists():
        print(f"ERROR: Project directory does not exist: {project_dir}")
        sys.exit(1)

    # Fetch project context
    project_context = fetch_project_context(task.get("project_id", 0))

    # Show task info
    complexity = get_task_complexity(task)
    mode_label = "Dev+Test loop" if args.dev_test else f"{args.role} (single agent)"
    print(f"\nTask #{task['id']}: {task['title']}")
    print(f"Project: {task.get('project_name', 'Unknown')}")
    print(f"Priority: {task.get('priority', 'medium')}")
    print(f"Complexity: {complexity}")
    print(f"Mode: {mode_label}")
    print(f"Directory: {project_dir}")

    if args.dev_test:
        dev_model = get_role_model("developer", args, task=task)
        dev_turns = get_role_turns("developer", args, task=task)
        tester_model = get_role_model("tester", args, task=task)
        tester_turns = get_role_turns("tester", args, task=task)
        print(f"Developer: model={dev_model}, turns={dev_turns}")
        print(f"Tester: model={tester_model}, turns={tester_turns}")
        print(f"Max cycles: {MAX_DEV_TEST_CYCLES}")
        print(f"Compaction: Dev >= {DEV_COMPACTION_THRESHOLD} turns, "
              f"Tester >= {TESTER_COMPACTION_THRESHOLD} turns")
        # Check for checkpoint
        cp_text, cp_attempt = load_checkpoint(task['id'], role="developer")
        if cp_text:
            print(f"Checkpoint: Found from attempt #{cp_attempt} ({len(cp_text)} chars) — will auto-resume")
    else:
        role_model = get_role_model(args.role, args, task=task)
        role_turns = get_role_turns(args.role, args, task=task)
        print(f"Model: {role_model}")
        print(f"Max turns: {role_turns}")
        print(f"Max retries: {args.retries}")

    if args.dry_run:
        # Build a sample prompt to show size
        system_prompt = build_system_prompt(task, project_context, project_dir, role="developer")
        dry_model = get_role_model("developer", args, task=task)
        dry_turns = get_role_turns("developer", args, task=task)
        cmd = build_cli_command(system_prompt, project_dir, dry_turns, dry_model, role="developer")

        print(f"\n--- DRY RUN ---")
        print(f"System prompt: {len(system_prompt)} chars")
        print(f"Command ({len(cmd)} args):")
        for i, part in enumerate(cmd):
            if i > 0 and cmd[i - 1] == "--append-system-prompt":
                print(f"  [system prompt: {len(part)} chars]")
            elif len(part) > 100:
                print(f"  {part[:100]}...")
            else:
                print(f"  {part}")

        if args.dev_test:
            print(f"\nDev-Test loop would run up to {MAX_DEV_TEST_CYCLES} cycles.")
            print(f"Each cycle: Developer agent -> Tester agent -> feedback loop.")

        print("\n--- END DRY RUN ---")
        return

    # Confirm before running
    print(f"\nDescription: {task.get('description', 'No description')[:200]}")
    if not args.yes:
        response = input("\nProceed? (y/n): ").strip().lower()
        if response != "y":
            print("Aborted.")
            return

    # --- Execute ---

    if args.dev_test:
        # Dev+Tester iteration loop (Phase 2)
        print(f"\nStarting Dev+Test loop (max {MAX_DEV_TEST_CYCLES} cycles)...")
        result, cycles, outcome = await run_dev_test_loop(
            task, project_dir, project_context, args,
        )

        # Orchestrator-side DB update (don't rely on agent)
        update_task_status(task["id"], outcome)

        # Optional security review after successful dev-test
        security_review_enabled = args.security_review
        if security_review_enabled is None:
            # Check dispatch config (already loaded globally on args)
            dc = getattr(args, "dispatch_config", None) or {}
            security_review_enabled = dc.get("security_review", False)

        if security_review_enabled and outcome in ("tests_passed", "no_tests"):
            sec_result = await run_security_review(task, project_dir, project_context, args)


        # Verify the task status in TheForge
        verified, verify_msg = verify_task_updated(task["id"])

        # Print loop summary
        print_dev_test_summary(task, result, cycles, outcome, verified, verify_msg)

    else:
        # Single-agent mode (Phase 1 — with model tiering)
        system_prompt = build_system_prompt(
            task, project_context, project_dir, role=args.role,
        )
        role_turns = get_role_turns(args.role, args, task=task)
        role_model = get_role_model(args.role, args, task=task)
        cmd = build_cli_command(
            system_prompt, project_dir, role_turns, role_model, role=args.role,
        )
        print(f"System prompt: {len(system_prompt)} chars")

        print(f"\nStarting {args.role} agent...")
        result, attempts = await run_agent_with_retries(cmd, task, args.retries)

        # Orchestrator-side DB update for single-agent mode
        single_outcome = "tests_passed" if result["success"] else "developer_failed"
        update_task_status(task["id"], single_outcome)

        # Verify the task status in TheForge
        verified, verify_msg = verify_task_updated(task["id"])

        # Print summary
        print_summary(task, result, verified, verify_msg)
        if attempts > 1:
            print(f"  Attempts: {attempts}/{args.retries}")


def main():
    """Entry point that runs the async main."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
