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
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# Force unbuffered output so logs are visible in real-time via nohup/SSH
os.environ["PYTHONUNBUFFERED"] = "1"

# Import ForgeSmith functions for lesson injection
try:
    from forgesmith import get_relevant_lessons
except ImportError:
    # Fallback if forgesmith is not available
    def get_relevant_lessons(role=None, error_type=None, limit=5):
        return []


# --- Constants ---

THEFORGE_DB = Path(r"TheForge\theforge.db")
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
PROCESS_TIMEOUT = 3600  # 60 minutes

# Per-role turn limits (used when dispatch config or CLI doesn't specify)
DEFAULT_ROLE_TURNS = {
    "developer": 40,
    "tester": 15,
    "security-reviewer": 30,
    "planner": 20,
    "evaluator": 15,
    "frontend-designer": 35,
    "integration-tester": 20,
    "debugger": 30,
    "code-reviewer": 20,
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

# Dynamic turn budget settings
DYNAMIC_BUDGET_START_RATIO = 0.8   # Start agents at 60% of their max_turns budget
DYNAMIC_BUDGET_MIN_TURNS = 15      # Minimum starting budget regardless of ratio
DYNAMIC_BUDGET_EXTEND_TURNS = 10   # Extra turns granted when agent reports FILES_CHANGED
DYNAMIC_BUDGET_BLOCKED_RATIO = 0.5 # Reduce remaining budget by 50% on RESULT: blocked

# Default model per role (overridden by dispatch_config per-role or per-complexity keys)
DEFAULT_ROLE_MODELS = {
    "developer": "opus",
    "tester": "sonnet",
    "security-reviewer": "opus",
    "planner": "opus",
    "evaluator": "sonnet",
    "frontend-designer": "opus",
    "integration-tester": "sonnet",
    "debugger": "opus",
    "code-reviewer": "sonnet",
}

# Dev+Tester loop constants
MAX_DEV_TEST_CYCLES = 5
DEV_COMPACTION_THRESHOLD = 10    # turns before compacting developer
TESTER_COMPACTION_THRESHOLD = 6  # turns before compacting tester
NO_PROGRESS_LIMIT = 2            # consecutive no-change runs before blocking
MAX_CONTINUATIONS = 3            # auto-retries when developer runs out of turns/timeout

# Early termination: detect stuck agents mid-run and kill before wasting turns
EARLY_TERM_WARN_TURNS = 25       # turns with no Edit/Write before injecting warning
EARLY_TERM_KILL_TURNS = 40       # turns with no Edit/Write before killing agent
EARLY_TERM_STUCK_PHRASES = [
    "i am unable to",
    "i cannot",
    "i'm unable to",
    "i'm not able to",
    "i don't have access",
    "i do not have access",
    "this is beyond my capabilities",
    "i cannot complete this task",
    "i'm stuck",
    "i am stuck",
]
# Roles that legitimately produce no file changes (research/planning tasks)
EARLY_TERM_EXEMPT_ROLES = {"planner", "evaluator", "security-reviewer", "code-reviewer", "researcher"}

# Manager mode constants (Phase 3)
MAX_MANAGER_ROUNDS = 3       # max plan-execute-evaluate rounds
MAX_TASKS_PER_PLAN = 8       # planner can't create more than this
MAX_FOLLOWUP_TASKS = 4       # evaluator can't create more than this per round

# Project codenames mapped to their synced storage directories
PROJECT_DIRS = {
    "stampede": r"usb-duplicator",
    "folder2flash": r"USBCopier",
    "magnetype": r"TextToSTL-NamepalteWithMagnets",
    "youtubedownloader": r"YouTubeDownloader",
    "cascade pro": r"CascadePro",
    "marketeer": r"marketeer",
    "arrmada": r"media-server-setup",
    "claudestick": r"claude-portable",
    "forgemind": r"TheForge",
    "codecourier": r"CodeCourier\CodeCourier",
    "whydah": r"Whydah",
    "wipestation": r"WipeStation",
    "fellowshipfirst": r"FellowshipFirst",
    "doge-habeus": r"DOGE-Habeus - POH-POS",
    "hoverfly": r"Hoverfly",
    "forgeteam": r"ForgeTeam",
    "forgebridge": r"ForgeBridge",
    "localllm-setup": r"LocalLLM-Setup",
    "loupe": r"MTG-Kiosk",
}

# For sorting text-based priority values
PRIORITY_ORDER = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}

# Default GitHub owner for --setup-repos
GITHUB_OWNER = "[OWNER]"


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


def record_agent_run(task, result, outcome, role="developer", model="opus",
                     max_turns=25, cycle_number=1, continuation_count=0, output=None):
    """Record agent execution telemetry to TheForge agent_runs table.

    Never crashes the orchestrator — all errors are logged and swallowed.
    Reads turns_allocated from result dict if available (set by dynamic budget system).
    """
    try:
        task_id = task.get("id") if isinstance(task, dict) else task
        project_id = task.get("project_id") if isinstance(task, dict) else None
        complexity = get_task_complexity(task) if isinstance(task, dict) else None
        success = 1 if outcome in ("tests_passed", "no_tests") else 0
        num_turns = result.get("num_turns", 0) if isinstance(result, dict) else 0
        duration = result.get("duration", 0) if isinstance(result, dict) else 0
        cost = result.get("cost") if isinstance(result, dict) else None
        errors = result.get("errors", []) if isinstance(result, dict) else []
        # Dynamic budget: read turns_allocated from result (set by run_dev_test_loop)
        turns_allocated = result.get("turns_allocated") if isinstance(result, dict) else None
        error_type = None
        error_summary = None

        # Early termination gets priority for error_type/error_summary
        if isinstance(result, dict) and result.get("early_terminated"):
            error_type = "early_terminated"
            error_summary = result.get("early_term_reason", "early termination")[:500]
        elif errors:
            error_summary = errors[0][:500] if errors[0] else None
            if "timed out" in (error_summary or "").lower():
                error_type = "timeout"
            elif "max_turns" in (error_summary or "").lower():
                error_type = "max_turns"
            elif "loop detected" in (error_summary or "").lower():
                error_type = "loop_detected"
            else:
                error_type = "agent_error"
        files_changed = result.get("files_changed_count", 0) if isinstance(result, dict) else 0

        conn = get_db_connection(write=True)
        conn.execute(
            """INSERT INTO agent_runs
               (task_id, project_id, role, model, complexity, num_turns,
                max_turns_allowed, duration_seconds, cost_usd, outcome,
                success, cycle_number, continuation_count, files_changed_count,
                error_type, error_summary, turns_allocated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, project_id, role, model, complexity, num_turns,
             max_turns, duration, cost, outcome,
             success, cycle_number, continuation_count, files_changed,
             error_type, error_summary, turns_allocated),
        )
        conn.commit()
        conn.close()
        budget_info = f", allocated={turns_allocated}" if turns_allocated else ""
        log(f"  [Telemetry] Recorded agent run: role={role}, outcome={outcome}, "
            f"turns={num_turns}/{max_turns}{budget_info}, duration={duration:.0f}s", output)
    except Exception as e:
        log(f"  [Telemetry] WARNING: Failed to record agent run: {e}", output)


# --- Reflexion (Post-task self-reflection for learning) ---

# Reflexion prompt — asks for specific, actionable self-reflection
REFLEXION_PROMPT = (
    "Reflect on this task. What approach did you take? What worked? "
    "What did not? What would you do differently next time? "
    "Be specific and concise (3-5 sentences). Reference exact files, "
    "error messages, tools, or strategies."
)

# Initial Q-value for new episodes (neutral prior)
INITIAL_Q_VALUE = 0.5


def ensure_agent_episodes_table():
    """Create agent_episodes table if it does not exist.

    Idempotent — safe to call every time.
    Also adds times_injected column if missing (for episode injection tracking).
    """
    try:
        conn = get_db_connection(write=True)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                role TEXT,
                task_type TEXT,
                project_id INTEGER,
                approach_summary TEXT,
                turns_used INTEGER,
                outcome TEXT,
                error_patterns TEXT,
                reflection TEXT,
                q_value REAL DEFAULT 0.5,
                times_injected INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Add times_injected column if table already existed without it
        try:
            conn.execute("ALTER TABLE agent_episodes ADD COLUMN times_injected INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  [Reflexion] WARNING: Could not ensure agent_episodes table: {e}")


def parse_reflection(result_text):
    """Extract REFLECTION text from agent structured output.

    Looks for a REFLECTION: line and captures all text until the next
    known section marker or end of text. Returns None if not found.
    """
    if not result_text:
        return None

    lines = result_text.splitlines()
    reflection_lines = []
    in_reflection = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("REFLECTION:"):
            in_reflection = True
            # Grab inline value after "REFLECTION:"
            value = stripped.split(":", 1)[1].strip()
            if value and value.lower() != "none":
                reflection_lines.append(value)
            continue

        if in_reflection:
            # Stop at next known section marker
            if any(stripped.startswith(marker) for marker in (
                "RESULT:", "SUMMARY:", "FILES_CHANGED:", "DECISIONS:",
                "BLOCKERS:", "```",
            )):
                break
            # Collect continuation lines (including bullet points)
            if stripped:
                reflection_lines.append(stripped)

    reflection = " ".join(reflection_lines).strip()
    return reflection if reflection else None


def parse_approach_summary(result_text):
    """Extract SUMMARY text from agent output for the episode record."""
    if not result_text:
        return None

    for line in result_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("SUMMARY:"):
            value = stripped.split(":", 1)[1].strip()
            return value if value else None

    return None


def parse_error_patterns(result):
    """Extract error patterns from agent result for episode record."""
    errors = result.get("errors", []) if isinstance(result, dict) else []
    if not errors:
        return None
    # Deduplicate and truncate
    unique = list(dict.fromkeys(errors))
    return "; ".join(e[:200] for e in unique[:5])


def compute_initial_q_value(outcome):
    """Set initial Q-value based on task outcome.

    Success starts higher (0.7), failure starts lower (0.3),
    partial/blocked at neutral (0.5).
    """
    if outcome in ("tests_passed", "no_tests"):
        return 0.7
    elif outcome in ("developer_failed", "cycles_exhausted"):
        return 0.3
    else:
        # blocked, timeout, no_progress, etc.
        return 0.4


def record_agent_episode(task, result, outcome, role="developer", output=None):
    """Store a Reflexion episode in the agent_episodes table.

    Extracts reflection from agent output. If no reflection found in
    the output, records the episode with a null reflection (the
    orchestrator will attempt a standalone reflexion call separately).

    Never crashes the orchestrator — all errors are logged and swallowed.
    """
    try:
        ensure_agent_episodes_table()

        task_id = task.get("id") if isinstance(task, dict) else task
        project_id = task.get("project_id") if isinstance(task, dict) else None
        task_type = task.get("role") or role if isinstance(task, dict) else role
        result_text = result.get("result_text", "") if isinstance(result, dict) else ""
        num_turns = result.get("num_turns", 0) if isinstance(result, dict) else 0

        reflection = parse_reflection(result_text)
        approach = parse_approach_summary(result_text)
        error_patterns = parse_error_patterns(result)
        q_value = compute_initial_q_value(outcome)

        conn = get_db_connection(write=True)
        conn.execute(
            """INSERT INTO agent_episodes
               (task_id, role, task_type, project_id, approach_summary,
                turns_used, outcome, error_patterns, reflection, q_value)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, role, task_type, project_id, approach,
             num_turns, outcome, error_patterns, reflection, q_value),
        )
        conn.commit()
        conn.close()

        if reflection:
            # Truncate for log display
            preview = reflection[:120] + "..." if len(reflection) > 120 else reflection
            log(f"  [Reflexion] Recorded episode with reflection: {preview}", output)
        else:
            log(f"  [Reflexion] Recorded episode (no reflection in output)", output)

    except Exception as e:
        log(f"  [Reflexion] WARNING: Failed to record episode: {e}", output)


async def run_reflexion_agent(task, result, outcome, role="developer", output=None):
    """Spawn a lightweight agent to generate reflection when not in output.

    This is a fallback — if the agent included REFLECTION: in its
    structured output, we already have it. This function is only called
    when parse_reflection() returned None.

    Uses minimal turns (max 2) and sonnet model to keep cost low.
    The reflection is stored back into the most recent agent_episode.
    """
    try:
        task_id = task.get("id") if isinstance(task, dict) else task
        task_title = task.get("title", "unknown") if isinstance(task, dict) else "unknown"
        result_text = result.get("result_text", "") if isinstance(result, dict) else ""
        num_turns = result.get("num_turns", 0) if isinstance(result, dict) else 0

        # Build a concise context for the reflection agent
        # Use last 1500 chars of output to stay within prompt limits
        output_tail = result_text[-1500:] if len(result_text) > 1500 else result_text

        reflection_prompt = (
            f"You are reflecting on a completed task.\n\n"
            f"Task: #{task_id} - {task_title}\n"
            f"Role: {role}\n"
            f"Outcome: {outcome}\n"
            f"Turns used: {num_turns}\n\n"
            f"Agent output (tail):\n{output_tail}\n\n"
            f"{REFLEXION_PROMPT}\n\n"
            f"Respond with ONLY your reflection text (3-5 sentences). "
            f"No preamble, no formatting, no markdown."
        )

        cmd = [
            "claude",
            "-p", reflection_prompt,
            "--output-format", "json",
            "--model", "sonnet",
            "--max-turns", "2",
            "--no-session-persistence",
        ]

        log(f"  [Reflexion] Spawning reflection agent for task #{task_id}...", output)
        ref_result = await run_agent(cmd, timeout=60)

        if not ref_result.get("success"):
            log(f"  [Reflexion] Reflection agent failed: {ref_result.get('errors', [])}", output)
            return

        reflection_text = ref_result.get("result_text", "").strip()
        if not reflection_text or len(reflection_text) < 20:
            log(f"  [Reflexion] Reflection too short, discarding.", output)
            return

        # Strip any JSON wrapper if present
        try:
            parsed = json.loads(reflection_text)
            if isinstance(parsed, dict) and "result" in parsed:
                reflection_text = parsed["result"].strip()
        except (json.JSONDecodeError, KeyError):
            pass  # not JSON, use raw text

        # Update the most recent episode for this task (subquery for portability)
        conn = get_db_connection(write=True)
        conn.execute(
            """UPDATE agent_episodes SET reflection = ?
               WHERE id = (
                   SELECT id FROM agent_episodes
                   WHERE task_id = ? AND reflection IS NULL
                   ORDER BY id DESC LIMIT 1
               )""",
            (reflection_text, task_id),
        )
        conn.commit()
        conn.close()

        preview = reflection_text[:120] + "..." if len(reflection_text) > 120 else reflection_text
        log(f"  [Reflexion] Captured reflection: {preview}", output)

    except Exception as e:
        log(f"  [Reflexion] WARNING: Standalone reflection failed: {e}", output)


async def maybe_run_reflexion(task, result, outcome, role="developer", output=None):
    """Record episode and optionally spawn reflection agent.

    This is the main entry point for the Reflexion pattern. Call this
    after record_agent_run() at every task completion point.

    Flow:
    1. Record the episode (extracts reflection from output if present)
    2. If no reflection was found in output, spawn lightweight agent
    """
    record_agent_episode(task, result, outcome, role=role, output=output)

    # Check if reflection was captured from the structured output
    result_text = result.get("result_text", "") if isinstance(result, dict) else ""
    if not parse_reflection(result_text):
        await run_reflexion_agent(task, result, outcome, role=role, output=output)


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


def format_lessons_for_injection(lessons):
    """Format lessons_learned for injection into agent prompts.

    Args:
        lessons: List of lesson dicts from get_relevant_lessons

    Returns:
        Formatted string with lessons under "## Lessons from Previous Runs" heading
    """
    if not lessons:
        return ""

    lines = ["## Lessons from Previous Runs", ""]
    for lesson in lessons:
        # Format as bullet point with lesson text
        lines.append(f"- {lesson['lesson']}")
        # Add context if available (error signature, times seen)
        if lesson.get('error_signature'):
            lines.append(f"  (Error: {lesson['error_signature']}, seen {lesson['times_seen']}x)")

    return "\n".join(lines)


def update_lesson_injection_count(lesson_ids):
    """Increment times_injected counter for the given lesson IDs.

    Args:
        lesson_ids: List of lesson IDs to update
    """
    if not lesson_ids:
        return

    try:
        conn = sqlite3.connect(THEFORGE_DB)
        placeholders = ",".join("?" * len(lesson_ids))
        conn.execute(
            f"UPDATE lessons_learned SET times_injected = times_injected + 1 WHERE id IN ({placeholders})",
            lesson_ids
        )
        conn.commit()
        conn.close()
    except Exception as e:
        # Don't fail the orchestrator if lesson update fails
        print(f"Warning: Failed to update lesson injection count: {e}")


# --- Episode Injection (MemRL pattern) ---

# Track which episode IDs were injected per task_id, so we can update q_values
# after the task completes. Keyed by task_id, value is list of episode IDs.
_injected_episodes_by_task = {}

def get_relevant_episodes(role, project_id, task_type=None, min_q_value=0.3, limit=3):
    """Fetch relevant past episodes for injection into agent prompts.

    Matches by: same role + same project + optionally similar task_type.
    Filters by q_value > min_q_value (only inject useful experiences).
    Returns top episodes ordered by q_value descending.

    Args:
        role: Agent role (e.g. 'developer', 'tester')
        project_id: Project ID to match episodes from
        task_type: Optional task type for similarity matching
        min_q_value: Minimum q_value threshold (default 0.3)
        limit: Maximum episodes to return (default 3)

    Returns:
        List of episode dicts with id, approach_summary, outcome, reflection, q_value
    """
    try:
        conn = get_db_connection()
        # Primary match: same role + same project + q_value above threshold + has reflection
        rows = conn.execute(
            """SELECT id, task_id, task_type, approach_summary, outcome,
                      reflection, q_value, turns_used
               FROM agent_episodes
               WHERE role = ? AND project_id = ? AND q_value > ?
                 AND reflection IS NOT NULL AND reflection != ''
               ORDER BY q_value DESC, created_at DESC
               LIMIT ?""",
            (role, project_id, min_q_value, limit),
        ).fetchall()

        episodes = [dict(r) for r in rows]

        # If we got fewer than limit, try matching by role + task_type across projects
        if len(episodes) < limit and task_type:
            existing_ids = {e["id"] for e in episodes}
            remaining = limit - len(episodes)
            cross_rows = conn.execute(
                """SELECT id, task_id, task_type, approach_summary, outcome,
                          reflection, q_value, turns_used
                   FROM agent_episodes
                   WHERE role = ? AND task_type = ? AND q_value > ?
                     AND reflection IS NOT NULL AND reflection != ''
                   ORDER BY q_value DESC, created_at DESC
                   LIMIT ?""",
                (role, task_type, min_q_value, remaining + len(existing_ids)),
            ).fetchall()
            for r in cross_rows:
                if dict(r)["id"] not in existing_ids and len(episodes) < limit:
                    episodes.append(dict(r))
                    existing_ids.add(dict(r)["id"])

        conn.close()
        return episodes
    except Exception as e:
        print(f"Warning: Failed to fetch relevant episodes: {e}")
        return []


def format_episodes_for_injection(episodes):
    """Format agent episodes for injection into agent prompts.

    Args:
        episodes: List of episode dicts from get_relevant_episodes

    Returns:
        Formatted string under "## Past Experience" heading (2-3 sentences each)
    """
    if not episodes:
        return ""

    lines = ["## Past Experience", ""]
    for ep in episodes:
        summary = ep.get("approach_summary") or "No summary"
        outcome = ep.get("outcome", "unknown")
        reflection = ep.get("reflection") or "No lesson recorded"
        # Truncate to keep injected text short
        if len(summary) > 120:
            summary = summary[:117] + "..."
        if len(reflection) > 150:
            reflection = reflection[:147] + "..."
        lines.append(
            f"- Previous similar task: {summary}. "
            f"Outcome: {outcome}. Lesson: {reflection}"
        )

    return "\n".join(lines)


def update_episode_injection_count(episode_ids):
    """Increment times_injected counter for the given episode IDs.

    Args:
        episode_ids: List of episode IDs that were injected into a prompt
    """
    if not episode_ids:
        return

    try:
        ensure_agent_episodes_table()
        conn = get_db_connection(write=True)
        placeholders = ",".join("?" * len(episode_ids))
        conn.execute(
            f"UPDATE agent_episodes SET times_injected = times_injected + 1 WHERE id IN ({placeholders})",
            episode_ids
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Warning: Failed to update episode injection count: {e}")


def update_episode_q_values(injected_episode_ids, task_succeeded):
    """Update q_values of previously injected episodes based on task outcome.

    Implements the MemRL reward signal:
    - If task succeeded and injected episode was useful: q_value += 0.1
    - If task failed despite injected episode: q_value -= 0.05
    - Q-values are bounded to [0.0, 1.0]

    Args:
        injected_episode_ids: List of episode IDs that were injected before this task
        task_succeeded: Whether the task completed successfully
    """
    if not injected_episode_ids:
        return

    try:
        conn = get_db_connection(write=True)
        if task_succeeded:
            delta = 0.1
        else:
            delta = -0.05

        for ep_id in injected_episode_ids:
            # Bounded update: clamp to [0.0, 1.0]
            conn.execute(
                """UPDATE agent_episodes
                   SET q_value = MIN(1.0, MAX(0.0, q_value + ?))
                   WHERE id = ?""",
                (delta, ep_id),
            )

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Warning: Failed to update episode q_values: {e}")


def update_injected_episode_q_values_for_task(task_id, outcome, output=None):
    """Look up which episodes were injected for a task and update their q_values.

    Called after task completion. Uses the _injected_episodes_by_task tracker
    to find which episodes were injected, then applies the MemRL reward signal.

    Args:
        task_id: The task ID that just completed
        outcome: The task outcome string (e.g. 'tests_passed', 'developer_failed')
        output: Optional output buffer for logging
    """
    ep_ids = _injected_episodes_by_task.pop(task_id, [])
    if not ep_ids:
        return

    task_succeeded = outcome in ("tests_passed", "no_tests")
    update_episode_q_values(ep_ids, task_succeeded)

    delta = "+0.1" if task_succeeded else "-0.05"
    log(f"  [MemRL] Updated q_values ({delta}) for {len(ep_ids)} injected episodes: {ep_ids}", output)


def build_system_prompt(task, project_context, project_dir, role="developer",
                        extra_context="", dispatch_config=None, error_type=None):
    """Read _common.md + role prompt, replace placeholders, append task prompt.

    extra_context: optional string appended after the task prompt (used for
    compaction history and test failure feedback in dev-test loop).
    dispatch_config: optional config dict for task-type-specific prompt injection.
    error_type: optional error type to filter relevant lessons (e.g. 'timeout', 'max_turns').
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

    # Inject lessons learned from ForgeSmith (after ForgeSmith Tuning section)
    lessons = get_relevant_lessons(role=role, error_type=error_type, limit=3)
    if lessons:
        lessons_text = format_lessons_for_injection(lessons)
        prompt = prompt + "\n\n" + lessons_text
        # Update times_injected counter for each lesson
        update_lesson_injection_count([l["id"] for l in lessons])

    # Inject relevant past episodes (MemRL pattern)
    task_id = task.get("id") if isinstance(task, dict) else task
    project_id = task.get("project_id") if isinstance(task, dict) else None
    task_type = task.get("task_type", "feature") if isinstance(task, dict) else None
    if project_id:
        episodes = get_relevant_episodes(
            role=role, project_id=project_id, task_type=task_type,
            min_q_value=0.3, limit=3,
        )
        if episodes:
            episodes_text = format_episodes_for_injection(episodes)
            prompt = prompt + "\n\n" + episodes_text
            # Track injected episode IDs for q-value updates after task completion
            ep_ids = [ep["id"] for ep in episodes]
            _injected_episodes_by_task[task_id] = ep_ids
            update_episode_injection_count(ep_ids)

    # Inject task-type-specific guidance if available
    task_type_supplement = ""
    if dispatch_config and "task_type_prompts" in dispatch_config:
        task_type = task.get("task_type", "feature") or "feature"
        task_type_prompts = dispatch_config["task_type_prompts"]
        if task_type in task_type_prompts:
            task_type_supplement = (
                f"\n\n## Task Type Guidance ({task_type})\n\n"
                f"{task_type_prompts[task_type]}\n"
            )

    # Append task-specific instructions
    task_prompt = build_task_prompt(task, project_context, project_dir)
    prompt = prompt + "\n\n---\n\n" + task_prompt

    # Append task-type supplement after task prompt
    if task_type_supplement:
        prompt = prompt + task_type_supplement

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

def build_cli_command(system_prompt, project_dir, max_turns, model, role="developer",
                      streaming=False):
    """Build the claude CLI command as a list of arguments.

    Args:
        streaming: If True, use stream-json output format for real-time monitoring.
    """
    output_format = "stream-json" if streaming else "json"
    cmd = [
        "claude",
        "-p",
        f"Execute the task described in your system prompt. Work in: {project_dir}",
        "--output-format", output_format,
        "--model", model,
        "--max-turns", str(max_turns),
        "--no-session-persistence",
        "--append-system-prompt", system_prompt,
        "--mcp-config", str(MCP_CONFIG),
        "--add-dir", str(project_dir),
        "--permission-mode", "bypassPermissions",
    ]

    # stream-json requires --verbose
    if streaming:
        cmd.append("--verbose")

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


# --- Early Termination (Stuck Agent Detection) ---

def _check_stuck_phrases(text):
    """Check if text contains any stuck signal phrases.

    Returns the matched phrase or None.
    """
    text_lower = text.lower()
    for phrase in EARLY_TERM_STUCK_PHRASES:
        if phrase in text_lower:
            return phrase
    return None


def _check_repeated_tool_calls(tool_history, window=4):
    """Detect repeated identical tool calls within a sliding window.

    Returns True if the last `window` tool calls are all identical.
    """
    if len(tool_history) < window:
        return False
    recent = tool_history[-window:]
    return len(set(recent)) == 1


def _detect_tool_loop(tool_history, tool_errors, warn_threshold=3, terminate_threshold=5):
    """Detect when an agent repeats the same failing tool operation.

    Tracks consecutive repetitions of the same tool signature and returns
    the current loop status. Enhanced to consider error patterns - only
    triggers on failed operations, not successful ones.

    Args:
        tool_history: List of tool signatures (tool_name|params)
        tool_errors: List of error summaries (None if success, string if error)
        warn_threshold: Number of repetitions before warning (default 3)
        terminate_threshold: Number of repetitions before termination (default 5)

    Returns:
        tuple: (action, count, last_sig) where action is "ok", "warn", or "terminate"
    """
    if len(tool_history) < 2:
        return ("ok", 0, None)

    # Count consecutive occurrences of the last tool signature
    last_sig = tool_history[-1]
    last_error = tool_errors[-1] if tool_errors else None

    # If the last operation succeeded (no error), reset the loop counter
    # This prevents false positives when retrying after fixing a bug
    if not last_error:
        return ("ok", 0, last_sig)

    consecutive = 1
    consecutive_failures = 1  # count only failing operations

    for i in range(len(tool_history) - 2, -1, -1):
        if tool_history[i] == last_sig:
            consecutive += 1
            # Only count if this was also a failure
            if i < len(tool_errors) and tool_errors[i]:
                consecutive_failures += 1
        else:
            break

    # Use consecutive_failures for triggering warnings/termination
    if consecutive_failures >= terminate_threshold:
        return ("terminate", consecutive_failures, last_sig)
    elif consecutive_failures >= warn_threshold:
        return ("warn", consecutive_failures, last_sig)
    else:
        return ("ok", consecutive_failures, last_sig)


async def run_agent_streaming(cmd, role="developer", timeout=None, output=None, max_turns=None):
    """Spawn claude -p with stream-json output for real-time stuck detection.

    Monitors agent output turn-by-turn and terminates early if stuck signals
    are detected. Only applies file-change monitoring to non-exempt roles
    (developer, tester, debugger, etc.).

    Returns the same dict format as run_agent().
    """
    effective_timeout = timeout or PROCESS_TIMEOUT
    start_time = time.time()
    is_exempt = role in EARLY_TERM_EXEMPT_ROLES

    # Tracking state
    turn_count = 0
    turns_without_file_change = 0
    # Scale early termination with budget — larger budgets get more reading time
    effective_kill_turns = max(EARLY_TERM_KILL_TURNS, int((max_turns or EARLY_TERM_KILL_TURNS) * 0.85))
    effective_warn_turns = max(EARLY_TERM_WARN_TURNS, int(effective_kill_turns * 0.7))
    has_any_file_change = False
    tool_history = []          # list of "tool_name:key_input" strings
    tool_errors = []           # list of error strings (None if success, string if error)
    stuck_phrase_count = 0
    all_text_chunks = []       # accumulate assistant text for final result
    result_data = None         # the final "result" message from stream-json
    warning_injected = False
    loop_warning_injected = False  # track if we've warned about loop detection
    early_term_reason = None
    loop_detected_details = None  # store loop details for error_summary

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024,  # 4MB buffer for large file reads
        )
    except FileNotFoundError:
        return {
            "success": False,
            "result_text": "",
            "num_turns": 0,
            "duration": 0,
            "cost": None,
            "errors": ["'claude' command not found. Is Claude Code installed and on PATH?"],
        }

    try:
        # Read stdout line-by-line with overall timeout
        while True:
            elapsed = time.time() - start_time
            remaining = effective_timeout - elapsed
            if remaining <= 0:
                early_term_reason = f"Process timed out after {effective_timeout} seconds"
                break

            try:
                line_bytes = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=min(remaining, 120),  # also cap per-line wait
                )
            except asyncio.TimeoutError:
                early_term_reason = f"Process timed out after {effective_timeout} seconds"
                break

            if not line_bytes:
                # EOF — process finished
                break

            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            # Parse stream-json message
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            # --- Handle "result" message (final) ---
            if msg_type == "result":
                result_data = msg
                break

            # --- Handle "assistant" messages (agent turns) ---
            if msg_type == "assistant":
                message = msg.get("message", {})
                content_blocks = message.get("content", [])

                for block in content_blocks:
                    block_type = block.get("type", "")

                    if block_type == "text":
                        text = block.get("text", "")
                        all_text_chunks.append(text)

                        # Check for stuck phrases
                        matched = _check_stuck_phrases(text)
                        if matched:
                            stuck_phrase_count += 1
                            log(f"  [EarlyTerm] Stuck signal detected at turn ~{turn_count}: "
                                f"\"{matched}\" (count: {stuck_phrase_count})", output)
                            # 3 stuck phrases = terminate
                            if stuck_phrase_count >= 3:
                                early_term_reason = (
                                    f"Agent stuck: repeated stuck phrases "
                                    f"({stuck_phrase_count}x, last: \"{matched}\")"
                                )

                    elif block_type == "tool_use":
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        turn_count += 1

                        # Track file-modifying tools
                        if tool_name in ("Edit", "Write", "NotebookEdit"):
                            turns_without_file_change = 0
                            has_any_file_change = True
                        else:
                            if not is_exempt:
                                turns_without_file_change += 1

                        # Build tool signature for repetition detection
                        # Use tool name + first key param for fingerprinting
                        sig_parts = [tool_name]
                        if tool_name == "Bash":
                            sig_parts.append(str(tool_input.get("command", ""))[:80])
                        elif tool_name == "Read":
                            sig_parts.append(str(tool_input.get("file_path", "")))
                        elif tool_name == "Grep":
                            sig_parts.append(str(tool_input.get("pattern", "")))
                        elif tool_name == "Glob":
                            sig_parts.append(str(tool_input.get("pattern", "")))
                        else:
                            # Generic: use first key
                            first_val = next(iter(tool_input.values()), "") if tool_input else ""
                            sig_parts.append(str(first_val)[:80])

                        tool_sig = "|".join(sig_parts)
                        tool_history.append(tool_sig)

                        # Check for loop detection (repeated failing operations)
                        action, count, last_sig = _detect_tool_loop(
                            tool_history,
                            tool_errors,
                            warn_threshold=LOOP_WARNING_THRESHOLD,
                            terminate_threshold=LOOP_TERMINATE_THRESHOLD
                        )

                        if action == "terminate":
                            early_term_reason = (
                                f"Loop detected: agent repeated the same operation "
                                f"{count} times ({tool_name})"
                            )
                            log(f"  [LoopDetect] {early_term_reason}", output)
                        elif action == "warn" and not loop_warning_injected:
                            log(f"  [LoopDetect] WARNING: Repeated operation detected "
                                f"({count}x: {tool_name}). Try a different approach.", output)
                            loop_warning_injected = True

                        # File-change turn monitoring (non-exempt roles only)
                        if not is_exempt and turns_without_file_change > 0:
                            if (turns_without_file_change >= effective_warn_turns
                                    and not warning_injected):
                                log(f"  [EarlyTerm] WARNING: {turns_without_file_change} "
                                    f"turns without file changes (role={role}, "
                                    f"turn ~{turn_count})", output)
                                warning_injected = True

                            if turns_without_file_change >= effective_kill_turns:
                                early_term_reason = (
                                    f"Agent terminated: {turns_without_file_change} "
                                    f"consecutive turns without file changes"
                                )
                                log(f"  [EarlyTerm] {early_term_reason}", output)

                # If we found a reason to terminate, break out
                if early_term_reason:
                    break

            # --- Handle "user" messages (tool results) ---
            elif msg_type == "user":
                message = msg.get("message", {})
                content_blocks = message.get("content", [])

                for block in content_blocks:
                    block_type = block.get("type", "")

                    if block_type == "tool_result":
                        # Track whether this tool call resulted in an error
                        is_error = block.get("is_error", False)
                        content = block.get("content", "")

                        # Extract error message if present
                        error_text = None
                        if is_error:
                            # Content can be a string or a list of content blocks
                            if isinstance(content, str):
                                error_text = content[:200]  # truncate long errors
                            elif isinstance(content, list):
                                # Extract text from content blocks
                                texts = []
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        texts.append(c.get("text", ""))
                                if texts:
                                    error_text = " ".join(texts)[:200]

                        tool_errors.append(error_text)

    except Exception as e:
        early_term_reason = f"Streaming monitor error: {e}"

    # --- Kill process if still running ---
    if process.returncode is None:
        log(f"  [EarlyTerm] Killing agent process (reason: {early_term_reason})", output)
        process.kill()
        try:
            await asyncio.wait_for(process.communicate(), timeout=5)
        except Exception:
            pass

    duration = time.time() - start_time

    # --- Build result dict (same format as run_agent) ---
    result = {
        "success": False,
        "result_text": "",
        "num_turns": turn_count,
        "duration": duration,
        "cost": None,
        "errors": [],
    }

    if early_term_reason:
        result["errors"].append(early_term_reason)
        result["early_terminated"] = True
        result["early_term_reason"] = early_term_reason

    # If we got the final result message from stream-json, use it
    if result_data:
        final_text = result_data.get("result", "")
        # If final result lacks structured test markers but accumulated text has them,
        # use the full accumulated text (tester often outputs RESULT: block mid-conversation)
        if ("RESULT:" not in final_text and all_text_chunks
                and any("RESULT:" in chunk for chunk in all_text_chunks)):
            result["result_text"] = "\n".join(all_text_chunks)
        else:
            result["result_text"] = final_text
        result["num_turns"] = result_data.get("num_turns", turn_count)
        result["cost"] = result_data.get("total_cost_usd")

        subtype = result_data.get("subtype", "")
        if subtype == "error_max_turns":
            result["success"] = True
            result["errors"].append("Agent hit max turns limit")
        elif result_data.get("is_error"):
            result["errors"].append(
                f"Agent error: {result_data.get('result', 'unknown')}")
        else:
            result["success"] = not bool(early_term_reason)
    else:
        # No result message — build text from accumulated chunks
        result["result_text"] = "\n".join(all_text_chunks)

    # Read any remaining stderr
    try:
        stderr_bytes = await asyncio.wait_for(process.stderr.read(), timeout=2)
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        if stderr_text:
            result["errors"].append(f"stderr: {stderr_text}")
    except Exception:
        pass

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


# --- Loop Detection ---

# Thresholds for loop detection
LOOP_WARNING_THRESHOLD = 3   # inject "try different approach" warning
LOOP_TERMINATE_THRESHOLD = 5  # terminate agent early and mark blocked


class LoopDetector:
    """Detect when an agent repeats the same failing pattern across cycles.

    Tracks fingerprints of agent output (error messages, result status,
    blockers) and detects repetition. Legitimate retries (where the agent
    makes changes between attempts) are excluded from repetition counts.

    Usage:
        detector = LoopDetector()
        for cycle in ...:
            result = await run_agent(cmd)
            action = detector.record(result, cycle)
            if action == "terminate":
                break
            elif action == "warn":
                compaction_history.append(detector.warning_message())
    """

    def __init__(self, warning_threshold=LOOP_WARNING_THRESHOLD,
                 terminate_threshold=LOOP_TERMINATE_THRESHOLD):
        self.warning_threshold = warning_threshold
        self.terminate_threshold = terminate_threshold
        self.fingerprints = []      # ordered list of fingerprints per cycle
        self.consecutive_same = 0   # consecutive identical fingerprints
        self.last_fingerprint = None
        self.warned = False         # have we injected a warning already?

    def _fingerprint(self, result):
        """Extract a normalized fingerprint from agent output.

        The fingerprint captures the essential pattern of what the agent did
        and what went wrong. It includes:
        - The RESULT: line (success/blocked/failed)
        - Error messages from the result dict
        - The BLOCKERS: section content
        - The SUMMARY: line

        Files changed are used to detect legitimate retries — if files differ
        between cycles, the repetition counter resets.
        """
        text = result.get("result_text", "") if isinstance(result, dict) else ""
        errors = result.get("errors", []) if isinstance(result, dict) else []

        parts = []

        # Extract structured output markers
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("RESULT:"):
                parts.append(stripped.split(":", 1)[1].strip().lower())
            elif stripped.startswith("BLOCKERS:"):
                blocker_val = stripped.split(":", 1)[1].strip().lower()
                if blocker_val and blocker_val != "none":
                    parts.append(f"blocker:{blocker_val}")
            elif stripped.startswith("SUMMARY:"):
                parts.append(f"summary:{stripped.split(':', 1)[1].strip().lower()}")

        # Include error messages (normalized)
        for err in errors[:3]:
            normalized = err.lower().strip()[:200]
            parts.append(f"error:{normalized}")

        return "|".join(sorted(parts)) if parts else "empty"

    def _get_files_changed(self, result):
        """Extract FILES_CHANGED from result text for retry detection."""
        text = result.get("result_text", "") if isinstance(result, dict) else ""
        return parse_developer_output(text)

    def record(self, result, cycle):
        """Record a cycle result and return the recommended action.

        Returns:
            "ok"        - no loop detected, continue normally
            "warn"      - repetition detected (>=warning_threshold), inject warning
            "terminate" - severe repetition (>=terminate_threshold), stop the agent
        """
        fp = self._fingerprint(result)
        files = self._get_files_changed(result)
        self.fingerprints.append((cycle, fp, files))

        if fp == self.last_fingerprint:
            # Same pattern — but check if files changed (legitimate retry)
            prev_files = self.fingerprints[-2][2] if len(self.fingerprints) >= 2 else []
            if sorted(files) != sorted(prev_files) and files:
                # Different files touched — this is a real retry, reset counter
                self.consecutive_same = 1
            else:
                self.consecutive_same += 1
        else:
            self.consecutive_same = 1
            self.last_fingerprint = fp

        if self.consecutive_same >= self.terminate_threshold:
            return "terminate"
        elif self.consecutive_same >= self.warning_threshold and not self.warned:
            self.warned = True
            return "warn"

        return "ok"

    def warning_message(self):
        """Build a warning to inject into the agent's next prompt context."""
        return (
            "## LOOP DETECTED — Try a Different Approach\n\n"
            "The orchestrator has detected that you are repeating the same "
            "failing pattern for multiple consecutive cycles. Your last "
            f"{self.consecutive_same} attempts produced identical error "
            "signatures.\n\n"
            "**You MUST try a fundamentally different approach:**\n"
            "- If a file edit keeps failing, try a different file or strategy\n"
            "- If a build error persists, investigate the root cause instead of retrying\n"
            "- If you are blocked, report it as a blocker rather than retrying\n"
            "- If you tried approach A three times, try approach B or C\n\n"
            "**If you repeat the same approach again, the orchestrator will "
            "terminate your session and mark the task as blocked.**\n"
        )

    def termination_summary(self):
        """Build an error summary string for agent_runs.error_summary."""
        return (
            f"Loop detected: agent repeated the same failing pattern "
            f"{self.consecutive_same} times. Last fingerprint: "
            f"{self.last_fingerprint[:200] if self.last_fingerprint else 'unknown'}"
        )


# --- Dynamic Turn Budget ---

def calculate_dynamic_budget(max_turns):
    """Calculate the starting turn budget for an agent.

    Starts at DYNAMIC_BUDGET_START_RATIO of max_turns, with a floor of
    DYNAMIC_BUDGET_MIN_TURNS to ensure agents can at least read files.

    Returns (starting_budget, max_turns) tuple.
    """
    starting = max(DYNAMIC_BUDGET_MIN_TURNS, int(max_turns * DYNAMIC_BUDGET_START_RATIO))
    # Don't exceed the max
    starting = min(starting, max_turns)
    return starting, max_turns


def adjust_dynamic_budget(current_budget, max_turns, result_text):
    """Adjust dynamic turn budget based on agent output.

    - If FILES_CHANGED found in output: extend by DYNAMIC_BUDGET_EXTEND_TURNS (up to max)
    - If RESULT: blocked found in output: reduce remaining by DYNAMIC_BUDGET_BLOCKED_RATIO
    - Otherwise: no change

    Returns the new budget.
    """
    if not result_text:
        return current_budget

    result_text_lower = result_text.lower()

    # Check for RESULT: blocked — reduce budget
    if "result: blocked" in result_text_lower or "result:blocked" in result_text_lower:
        reduced = max(DYNAMIC_BUDGET_MIN_TURNS,
                      int(current_budget * DYNAMIC_BUDGET_BLOCKED_RATIO))
        return min(reduced, max_turns)

    # Check for FILES_CHANGED with actual content (not "none" or empty)
    files_changed_patterns = ["files_changed:", "files changed:"]
    has_files_changed = False
    for pattern in files_changed_patterns:
        idx = result_text_lower.find(pattern)
        if idx >= 0:
            # Extract the value after the marker
            after = result_text[idx + len(pattern):idx + len(pattern) + 200].strip()
            # Consider it real if it's not "none", empty, or just whitespace
            first_line = after.split("\n")[0].strip()
            if first_line and first_line.lower() not in ("none", "n/a", "no files", ""):
                has_files_changed = True
                break

    if has_files_changed:
        extended = min(current_budget + DYNAMIC_BUDGET_EXTEND_TURNS, max_turns)
        return extended

    return current_budget


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
    last_error_type = None   # track last error for lesson injection
    loop_detector = LoopDetector()  # detect repeated failing patterns

    # Reset status so orchestrator is authoritative
    conn = get_db_connection(write=True)
    conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    log(f"  [Setup] Task {task_id} status reset to in_progress (orchestrator manages lifecycle)", output)

    # Resolve model and turns using adaptive tiering
    complexity = get_task_complexity(task)
    # Use task-specified role if available, otherwise default to developer
    task_role = getattr(task, 'role', None) or (task.get('role') if isinstance(task, dict) else None) or "developer"
    dev_model = get_role_model(task_role, args, task=task)
    tester_model = get_role_model("tester", args, task=task)
    dev_turns_max = get_role_turns(task_role, args, task=task)
    tester_turns_max = get_role_turns("tester", args, task=task)

    # Dynamic turn budgets: start conservative, extend on progress
    dev_turns_allocated, _ = calculate_dynamic_budget(dev_turns_max)
    tester_turns_allocated, _ = calculate_dynamic_budget(tester_turns_max)

    log(f"  Task complexity: {complexity}", output)
    log(f"  Developer: model={dev_model}, budget={dev_turns_allocated}/{dev_turns_max} "
        f"(dynamic, min={DYNAMIC_BUDGET_MIN_TURNS})", output)
    log(f"  Tester: model={tester_model}, budget={tester_turns_allocated}/{tester_turns_max} "
        f"(dynamic)", output)

    # Check for checkpoint from a previous timed-out attempt
    checkpoint_text, prev_attempt = load_checkpoint(task_id, role=task_role)
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
        log(f"\n  [Cycle {cycle}] Running Developer agent "
            f"(budget: {dev_turns_allocated}/{dev_turns_max})...", output)

        # Build extra context from compaction history
        extra_context = "\n\n".join(compaction_history) if compaction_history else ""

        dev_prompt = build_system_prompt(
            task, project_context, project_dir,
            role=task_role, extra_context=extra_context,
            dispatch_config=getattr(args, "dispatch_config", None),
            error_type=last_error_type,
        )
        # Use streaming mode for early termination on non-exempt roles
        use_streaming = task_role not in EARLY_TERM_EXEMPT_ROLES
        dev_cmd = build_cli_command(
            dev_prompt, project_dir, dev_turns_allocated, dev_model, role=task_role,
            streaming=use_streaming,
        )

        if use_streaming:
            dev_result = await run_agent_streaming(
                dev_cmd, role=task_role, output=output, max_turns=dev_turns_allocated)
        else:
            dev_result = await run_agent(dev_cmd)
        # Tag result with dynamic budget info for telemetry
        dev_result["turns_allocated"] = dev_turns_allocated
        dev_result["turns_max"] = dev_turns_max
        total_duration += dev_result.get("duration", 0)
        if dev_result.get("cost"):
            total_cost += dev_result["cost"]

        # Check for early termination
        if dev_result.get("early_terminated"):
            reason = dev_result.get("early_term_reason", "unknown")
            log(f"  [Cycle {cycle}] Developer early-terminated: {reason}", output)
            return dev_result, cycle, "early_terminated"

        # Check for timeout or max_turns — save checkpoint for resume
        is_timeout = any("timed out" in e for e in dev_result.get("errors", []))
        is_max_turns = any("max turns" in e for e in dev_result.get("errors", []))

        if is_timeout or is_max_turns:
            reason = "timed out" if is_timeout else "hit max turns"
            # Track error type for lesson injection on next iteration
            last_error_type = "timeout" if is_timeout else "max_turns"
            continuation_count += 1
            log(f"  [Cycle {cycle}] Developer {reason}. "
                f"(continuation {continuation_count}/{MAX_CONTINUATIONS})", output)

            # Save checkpoint
            result_text = dev_result.get("result_text", "")
            if result_text:
                attempt_num = prev_attempt + cycle
                cp_path = save_checkpoint(task_id, attempt_num, result_text, role=task_role)
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

        if made_progress:
            # Clear error type on successful progress
            last_error_type = None

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

        # --- Dynamic Budget Adjustment ---
        prev_budget = dev_turns_allocated
        dev_turns_allocated = adjust_dynamic_budget(
            dev_turns_allocated, dev_turns_max,
            dev_result.get("result_text", ""))
        if dev_turns_allocated != prev_budget:
            log(f"  [DynBudget] Developer budget adjusted: {prev_budget} -> "
                f"{dev_turns_allocated}/{dev_turns_max}", output)

        # --- Loop Detection ---
        loop_action = loop_detector.record(dev_result, cycle)
        if loop_action == "terminate":
            log(f"  [Cycle {cycle}] LOOP DETECTED: Agent repeated the same failing "
                f"pattern {loop_detector.consecutive_same} times. Terminating early.", output)
            # Attach loop info to the result for telemetry
            dev_result.setdefault("errors", []).append(loop_detector.termination_summary())
            return dev_result, cycle, "loop_detected"
        elif loop_action == "warn":
            log(f"  [Cycle {cycle}] Loop warning: Agent has repeated the same pattern "
                f"{loop_detector.consecutive_same} times. Injecting 'try different approach' "
                f"guidance.", output)
            compaction_history.append(loop_detector.warning_message())
            # Log the warning in error_summary for telemetry
            dev_result.setdefault("errors", []).append(
                f"Loop warning: agent repeated same pattern "
                f"{loop_detector.consecutive_same} times (cycle {cycle})"
            )

        # --- Tester Phase ---
        log(f"\n  [Cycle {cycle}] Running Tester agent "
            f"(budget: {tester_turns_allocated}/{tester_turns_max})...", output)

        tester_prompt = build_system_prompt(
            task, project_context, project_dir, role="tester",
            dispatch_config=getattr(args, "dispatch_config", None),
        )
        tester_cmd = build_cli_command(
            tester_prompt, project_dir, tester_turns_allocated, tester_model, role="tester",
            streaming=True,
        )

        tester_result = await run_agent_streaming(
            tester_cmd, role="tester", output=output, max_turns=tester_turns_allocated)
        # Tag tester result with dynamic budget info for telemetry
        tester_result["turns_allocated"] = tester_turns_allocated
        tester_result["turns_max"] = tester_turns_max
        total_duration += tester_result.get("duration", 0)
        if tester_result.get("cost"):
            total_cost += tester_result["cost"]

        # Check for early termination (stuck tester)
        if tester_result.get("early_terminated"):
            reason = tester_result.get("early_term_reason", "unknown")
            log(f"  [Cycle {cycle}] Tester early-terminated: {reason}", output)
            return tester_result, cycle, "early_terminated"

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

        elif (test_outcome == "unknown" and test_results["tests_run"] == 0
              and test_results["tests_failed"] == 0):
            # Tester couldn't produce structured output and no tests actually ran
            log(f"  [Cycle {cycle}] Tester returned unknown with 0 tests. "
                f"Treating as no-tests.", output)
            clear_checkpoints(task_id)
            dev_result["cost"] = total_cost
            dev_result["duration"] = total_duration
            return dev_result, cycle, "no_tests"

        else:
            # test_outcome == "fail" (with actual test failures)
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
        dispatch_config=getattr(args, "dispatch_config", None),
    )
    sec_turns = get_role_turns("security-reviewer", args, task=task)
    sec_model = get_role_model("security-reviewer", args, task=task)
    sec_cmd = build_cli_command(
        sec_prompt, project_dir, sec_turns, sec_model, role="security-reviewer",
    )

    # Use security_review_timeout from dispatch config (default 15 min)
    dc = load_dispatch_config(None)
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
    for candidate in [
        r"C:\Program Files\Git\cmd",
        r"C:\Program Files\GitHub CLI",
    ]:
        if os.path.isdir(candidate) and candidate not in env.get("PATH", ""):
            extra_paths.append(candidate)
    if extra_paths:
        env["PATH"] = ";".join(extra_paths) + ";" + env.get("PATH", "")
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
    "max_concurrent": 8,
    "model": "sonnet",
    "max_turns": 25,
    "max_tasks_per_project": 3,
    "skip_projects": [],
    "priority_boost": {},
    "only_projects": [],
    "security_review": False,
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

        # ForgeSmith telemetry
        task_role = task.get("role") or "developer"
        record_agent_run(
            task, result, outcome, role=task_role,
            model=get_role_model(task_role, task_args, task=task),
            max_turns=get_role_turns(task_role, task_args, task=task),
            cycle_number=cycles, output=output,
        )

        # Reflexion: record episode and capture self-reflection
        await maybe_run_reflexion(task, result, outcome, role=task_role, output=output)

        # MemRL: update q_values of episodes that were injected into this task's prompt
        update_injected_episode_q_values_for_task(task_id, outcome, output=output)

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
    max_turns_val = config.get('max_turns', DEFAULT_MAX_TURNS)
    start_budget, _ = calculate_dynamic_budget(max_turns_val)
    print(f"  Max turns: {max_turns_val} (dynamic start: {start_budget})")
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
    # Dynamically discover available roles from prompts directory
    _available_roles = sorted([
        f.stem for f in (Path(__file__).parent / "prompts").glob("*.md")
        if not f.name.startswith("_")
    ]) if (Path(__file__).parent / "prompts").exists() else ["developer", "tester", "security-reviewer"]
    parser.add_argument("--role", default="developer", choices=_available_roles,
                        help=f"Agent role (available: {', '.join(_available_roles)}) (default: developer)")
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

    # --- Fetch task ---
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
        # Use task-specified role if available, otherwise default to developer
        task_role = getattr(task, 'role', None) or (task.get('role') if isinstance(task, dict) else None) or "developer"
        dev_model = get_role_model(task_role, args, task=task)
        dev_turns = get_role_turns("developer", args, task=task)
        tester_model = get_role_model("tester", args, task=task)
        tester_turns = get_role_turns("tester", args, task=task)
        dev_budget, _ = calculate_dynamic_budget(dev_turns)
        tester_budget, _ = calculate_dynamic_budget(tester_turns)
        print(f"Developer: model={dev_model}, budget={dev_budget}/{dev_turns} (dynamic)")
        print(f"Tester: model={tester_model}, budget={tester_budget}/{tester_turns} (dynamic)")
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
        role_budget, _ = calculate_dynamic_budget(role_turns)
        print(f"Model: {role_model}")
        print(f"Budget: {role_budget}/{role_turns} turns (dynamic)")
        print(f"Max retries: {args.retries}")

    if args.dry_run:
        # Build a sample prompt to show size
        system_prompt = build_system_prompt(task, project_context, project_dir, role="developer",
                                                  dispatch_config=getattr(args, "dispatch_config", None))
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

        # ForgeSmith telemetry
        task_role = task.get("role") or "developer"
        record_agent_run(
            task, result, outcome, role=task_role,
            model=get_role_model(task_role, args, task=task),
            max_turns=get_role_turns(task_role, args, task=task),
            cycle_number=cycles,
        )

        # Reflexion: record episode and capture self-reflection
        await maybe_run_reflexion(task, result, outcome, role=task_role)

        # MemRL: update q_values of episodes that were injected into this task's prompt
        update_injected_episode_q_values_for_task(task["id"], outcome)

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
        use_streaming = args.role not in EARLY_TERM_EXEMPT_ROLES
        system_prompt = build_system_prompt(
            task, project_context, project_dir, role=args.role,
            dispatch_config=getattr(args, "dispatch_config", None),
        )
        role_turns_max = get_role_turns(args.role, args, task=task)
        role_model = get_role_model(args.role, args, task=task)
        # Dynamic budget for single-agent mode
        role_turns_allocated, _ = calculate_dynamic_budget(role_turns_max)
        print(f"Dynamic budget: {role_turns_allocated}/{role_turns_max} turns")
        cmd = build_cli_command(
            system_prompt, project_dir, role_turns_allocated, role_model, role=args.role,
            streaming=use_streaming,
        )
        print(f"System prompt: {len(system_prompt)} chars")

        print(f"\nStarting {args.role} agent...")
        if use_streaming:
            # Streaming mode with early termination — no retries (kill is intentional)
            result = await run_agent_streaming(cmd, role=args.role)
            attempts = 1
        else:
            result, attempts = await run_agent_with_retries(cmd, task, args.retries)

        # Tag result with dynamic budget info for telemetry
        result["turns_allocated"] = role_turns_allocated
        result["turns_max"] = role_turns_max

        # Orchestrator-side DB update for single-agent mode
        if result.get("early_terminated"):
            single_outcome = "early_terminated"
        elif result["success"]:
            single_outcome = "tests_passed"
        else:
            single_outcome = "developer_failed"
        update_task_status(task["id"], single_outcome)

        # ForgeSmith telemetry
        record_agent_run(
            task, result, single_outcome, role=args.role,
            model=role_model, max_turns=role_turns_max,
        )

        # Reflexion: record episode and capture self-reflection
        await maybe_run_reflexion(task, result, single_outcome, role=args.role)

        # MemRL: update q_values of episodes that were injected into this task's prompt
        update_injected_episode_q_values_for_task(task["id"], single_outcome)

        # Verify the task status in TheForge
        verified, verify_msg = verify_task_updated(task["id"])

        # Print summary
        print_summary(task, result, verified, verify_msg)
        if attempts > 1:
            print(f"  Attempts: {attempts}/{args.retries}")




def main():
    """Entry point that runs the async main.

    For --project mode, loops until no more todo tasks remain.
    For --task/--tasks mode, runs once and exits.
    """
    # Check sys.argv to determine if --project mode (no parse_args needed)
    is_project_mode = "--project" in sys.argv and "--task" not in sys.argv and "--tasks" not in sys.argv

    if is_project_mode:
        # --project mode: loop through all todo tasks
        task_count = 0
        while True:
            try:
                asyncio.run(async_main())
                task_count += 1
                print(f"\n{'='*60}")
                print(f"Task complete ({task_count} so far). Checking for more...")
                print(f"{'='*60}\n")
            except SystemExit as e:
                if e.code == 0:
                    # Normal exit = no more tasks
                    print(f"\nAll done! Completed {task_count} tasks for project {args.project}.")
                    break
                else:
                    # Error exit
                    print(f"\nOrchestrator exited with code {e.code} after {task_count} tasks.")
                    sys.exit(e.code)
            except KeyboardInterrupt:
                print(f"\nInterrupted after {task_count} tasks.")
                sys.exit(0)
            except Exception as e:
                print(f"\nError after {task_count} tasks: {e}")
                print("Stopping orchestrator loop.")
                sys.exit(1)
    else:
        # Single task or parallel tasks mode: run once
        asyncio.run(async_main())


if __name__ == "__main__":
    main()
