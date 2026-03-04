"""
Itzamna Setup Wizard — Portable Installer for ForgeTeam

Interactive setup script that creates a fresh ForgeTeam installation:
- Checks prerequisites (Python, git, gh, claude, uvx)
- Creates directory structure
- Creates a fresh database with the full Itzamna schema
- Copies bundled ForgeTeam files (orchestrator, forgesmith, prompts, skills, config)
- Generates forge_config.json and mcp_config.json
- Sets up ForgeSmith nightly cron job for self-improvement
- Verifies the installation

All required ForgeTeam files are bundled in this repo — no external
dependencies on other repos needed.

Usage:
    python itzamna_setup.py

Stdlib only — no pip dependencies required.

Copyright 2026 Forgeborn
"""

import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

# --- Constants ---

SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_FILE = SCRIPT_DIR / "schema.sql"
# Source ForgeTeam files (bundled in this repo for standalone operation)
SOURCE_DIR = SCRIPT_DIR

BANNER = r"""
  ___  _
 |_ _|| |_  ____  __ _  _ __ ___   _ __    __ _
  | | | __|/_ / / _` || '_ ` _ \ | '_ \  / _` |
  | | | |_  / /_| (_| || | | | | || | | || (_| |
 |___| \__|/____|\__,_||_| |_| |_||_| |_| \__,_|

 ForgeTeam Portable Installer v1.0
 Mayan god of creation, writing, and knowledge
"""

MIN_PYTHON = (3, 10)


# --- Helpers ---

def print_header(text):
    """Print a section header."""
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}\n")


def print_step(num, text):
    """Print a numbered step."""
    print(f"\n  [{num}] {text}")


def prompt_input(message, default=None):
    """Prompt user for input with an optional default."""
    if default:
        user_input = input(f"  {message} [{default}]: ").strip()
        return user_input if user_input else default
    else:
        return input(f"  {message}: ").strip()


def prompt_yes_no(message, default=True):
    """Prompt for a yes/no answer."""
    suffix = "(Y/n)" if default else "(y/N)"
    response = input(f"  {message} {suffix}: ").strip().lower()
    if not response:
        return default
    return response in ("y", "yes")


def check_command(cmd, version_flag="--version"):
    """Check if a command is available and return its version string."""
    # Try the command directly first
    candidates = [cmd]
    # Also check common install locations (non-interactive shells may not have full PATH)
    if cmd in ("uvx", "uv", "claude"):
        candidates.extend([
            str(Path.home() / ".local" / "bin" / cmd),
            str(Path.home() / ".cargo" / "bin" / cmd),
            f"/usr/local/bin/{cmd}",
        ])
    for candidate in candidates:
        try:
            result = subprocess.run(
                [candidate, version_flag],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = result.stdout.strip() or result.stderr.strip()
            # Return first line only
            return output.split("\n")[0] if output else "installed"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def run_sql_file(db_path, sql_path):
    """Execute a SQL file against a SQLite database."""
    with open(sql_path, "r", encoding="utf-8") as f:
        sql = f.read()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


def count_db_objects(db_path):
    """Count tables, views, indexes, and triggers in the database."""
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        counts = {}
        for obj_type in ("table", "view", "index", "trigger"):
            cursor.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = ?",
                (obj_type,),
            )
            counts[obj_type] = cursor.fetchone()[0]
        return counts
    finally:
        conn.close()


# --- Setup Steps ---

def step_welcome():
    """Display the welcome banner."""
    print(BANNER)
    print("  This wizard will set up a fresh ForgeTeam installation.")
    print("  It creates the database, copies files, and generates config.")
    print()
    if not prompt_yes_no("Ready to begin?"):
        print("\n  Setup cancelled.")
        sys.exit(0)


def step_prerequisites():
    """Check all prerequisites are installed."""
    print_header("Step 1: Prerequisites Check")

    checks = [
        ("Python 3.10+", None, None),  # special handling
        ("git", "git", "--version"),
        ("gh (GitHub CLI)", "gh", "--version"),
        ("Claude Code CLI", "claude", "--version"),
        ("uvx / uv", "uvx", "--version"),
    ]

    results = []
    all_ok = True

    # Python version check
    py_ver = sys.version_info
    py_ok = py_ver >= MIN_PYTHON
    py_str = f"{py_ver.major}.{py_ver.minor}.{py_ver.micro}"
    status = "OK" if py_ok else "FAIL"
    results.append(("Python 3.10+", py_str, py_ok))
    if not py_ok:
        all_ok = False

    # External tool checks
    for name, cmd, flag in checks[1:]:
        version = check_command(cmd, flag)
        ok = version is not None
        results.append((name, version or "NOT FOUND", ok))
        if not ok:
            all_ok = False

    # Display results
    for name, version, ok in results:
        icon = "+" if ok else "X"
        print(f"  [{icon}] {name}: {version}")

    if not all_ok:
        print("\n  WARNING: Some prerequisites are missing.")
        print("  ForgeTeam requires all of the above to function.")
        if not prompt_yes_no("Continue anyway?", default=False):
            print("\n  Setup cancelled. Install missing prerequisites and retry.")
            sys.exit(1)
    else:
        print("\n  All prerequisites found.")

    return all_ok


def step_install_path():
    """Prompt user for the installation directory."""
    print_header("Step 2: Install Path")

    default_base = str(Path.home() / "ForgeTeam")

    base_dir = prompt_input("Install directory", default=default_base)
    base_path = Path(base_dir).resolve()

    if base_path.exists() and any(base_path.iterdir()):
        print(f"\n  WARNING: Directory already exists and is not empty: {base_path}")
        if not prompt_yes_no("Continue and overwrite?", default=False):
            print("  Setup cancelled.")
            sys.exit(1)

    # Create directory structure
    dirs = [
        base_path,
        base_path / "prompts",
        base_path / "skills",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        print(f"  Created: {d}")

    return base_path


def step_database(base_path):
    """Set up the database — create fresh, upgrade existing, or skip if current."""
    print_header("Step 3: Database Setup")

    db_name = prompt_input("Database filename", default="theforge.db")
    default_db_dir = str(base_path)
    db_dir = prompt_input("Database directory", default=default_db_dir)
    db_path = Path(db_dir).resolve() / db_name

    if db_path.exists():
        # --- Existing database: check version and offer upgrade ---
        from db_migrate import get_effective_version, run_migrations, CURRENT_VERSION

        conn = sqlite3.connect(str(db_path))
        try:
            db_version = get_effective_version(conn)
        finally:
            conn.close()

        if db_version >= CURRENT_VERSION:
            print(f"\n  Database is up to date (v{db_version}).")
            print(f"  Location: {db_path}")
            return db_path

        # Upgrade needed
        print(f"\n  Existing database found at {db_path}")
        print(f"  Detected schema version: v{db_version} (current is v{CURRENT_VERSION})")
        print()

        if prompt_yes_no(f"Upgrade database from v{db_version} to v{CURRENT_VERSION}?"):
            success, from_ver, to_ver = run_migrations(db_path)
            if success:
                counts = count_db_objects(db_path)
                print(f"  Schema objects after upgrade:")
                print(f"    Tables:   {counts['table']}")
                print(f"    Views:    {counts['view']}")
                print(f"    Indexes:  {counts['index']}")
                print(f"    Triggers: {counts['trigger']}")
                return db_path
            else:
                print(f"  Migration failed at v{to_ver}. Check the backup file")
                print(f"  and error messages above.")
                if not prompt_yes_no("Continue setup with the current database?", default=False):
                    sys.exit(1)
                return db_path
        else:
            print("  Keeping existing database without upgrade.")
            return db_path

    # --- New database: create from schema.sql ---
    if not SCHEMA_FILE.exists():
        print(f"  ERROR: Schema file not found at {SCHEMA_FILE}")
        print("  Cannot create database without schema.sql.")
        sys.exit(1)

    print(f"  Creating Itzamna database at: {db_path}")
    run_sql_file(db_path, SCHEMA_FILE)

    # Verify
    counts = count_db_objects(db_path)
    print(f"  Itzamna database created successfully:")
    print(f"    Tables:   {counts['table']}")
    print(f"    Views:    {counts['view']}")
    print(f"    Indexes:  {counts['index']}")
    print(f"    Triggers: {counts['trigger']}")

    return db_path


def step_copy_files(base_path):
    """Copy ForgeTeam files to the install directory."""
    print_header("Step 4: Copy ForgeTeam Files")

    # All source files are bundled in this repo (SOURCE_DIR = SCRIPT_DIR)
    missing = []
    for name in ["forge_orchestrator.py", "forgesmith.py", "dispatch_config.json", "forgesmith_config.json"]:
        if not (SOURCE_DIR / name).exists():
            missing.append(name)
    if not (SOURCE_DIR / "prompts").exists():
        missing.append("prompts/")
    if missing:
        print(f"  ERROR: Missing bundled files: {', '.join(missing)}")
        print(f"  Expected in: {SOURCE_DIR}")
        print("  Re-clone the Itzamna repo to restore them.")
        return False

    # Files to copy
    copy_map = {
        "forge_orchestrator.py": SOURCE_DIR / "forge_orchestrator.py",
        "forgesmith.py": SOURCE_DIR / "forgesmith.py",
        "dispatch_config.json": SOURCE_DIR / "dispatch_config.json",
        "forgesmith_config.json": SOURCE_DIR / "forgesmith_config.json",
    }

    # Copy individual files
    for dest_name, src_path in copy_map.items():
        if src_path.exists():
            dest = base_path / dest_name
            shutil.copy2(str(src_path), str(dest))
            print(f"  Copied: {dest_name}")
        else:
            print(f"  SKIP: {src_path.name} (not found)")

    # Copy prompts directory
    src_prompts = SOURCE_DIR / "prompts"
    if src_prompts.exists():
        dest_prompts = base_path / "prompts"
        count = 0
        for md_file in src_prompts.glob("*.md"):
            shutil.copy2(str(md_file), str(dest_prompts / md_file.name))
            count += 1
        print(f"  Copied: {count} prompt files to prompts/")
    else:
        print("  SKIP: prompts/ directory (not found)")

    # Copy skills directory
    src_skills = SOURCE_DIR / "skills" / "security"
    if src_skills.exists():
        dest_skills = base_path / "skills" / "security"
        if dest_skills.exists():
            if dest_skills.is_symlink():
                dest_skills.unlink()  # safe: remove symlink only, not target
            else:
                shutil.rmtree(str(dest_skills))
        shutil.copytree(str(src_skills), str(dest_skills))
        skill_count = sum(1 for _ in dest_skills.rglob("*.md"))
        print(f"  Copied: skills/security/ ({skill_count} files)")
    else:
        print("  SKIP: skills/security/ directory (not found)")

    return True


def step_generate_config(base_path, db_path):
    """Generate forge_config.json."""
    print_header("Step 5: Generate Configuration")

    github_owner = prompt_input("GitHub username", default="YourGitHubUsername")

    config = {
        "theforge_db": str(db_path),
        "project_dirs": {},
        "github_owner": github_owner,
        "mcp_config": str(base_path / "mcp_config.json"),
        "prompts_dir": str(base_path / "prompts"),
    }

    config_path = base_path / "forge_config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)
    print(f"  Created: {config_path}")

    # Display config
    print(f"\n  Configuration:")
    for key, value in config.items():
        if key == "project_dirs":
            print(f"    {key}: {{}} (empty — add projects with --add-project)")
        else:
            print(f"    {key}: {value}")

    return config


def _resolve_uvx_path():
    """Find the full path to uvx, needed because MCP subprocesses may not inherit PATH."""
    uvx_path = shutil.which("uvx")
    if uvx_path:
        return str(Path(uvx_path).resolve())
    # Common locations to check
    candidates = [
        Path.home() / ".local" / "bin" / "uvx",
        Path.home() / ".cargo" / "bin" / "uvx",
        Path("/usr/local/bin/uvx"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    # Fallback — will likely fail but gives a clear error
    print("  WARNING: Could not find uvx. MCP server may fail to start.")
    print("  Install uv/uvx: curl -LsSf https://astral.sh/uv/install.sh | sh")
    return "uvx"


def step_generate_mcp_config(base_path, db_path):
    """Generate mcp_config.json for MCP server."""
    print_header("Step 6: Generate MCP Configuration")

    uvx_cmd = _resolve_uvx_path()

    mcp_config = {
        "mcpServers": {
            "theforge": {
                "type": "stdio",
                "command": uvx_cmd,
                "args": [
                    "mcp-server-sqlite",
                    "--db-path",
                    str(db_path),
                ],
            }
        }
    }

    mcp_path = base_path / "mcp_config.json"
    with open(mcp_path, "w", encoding="utf-8") as f:
        json.dump(mcp_config, f, indent=2)
    print(f"  Created: {mcp_path}")
    print(f"  MCP server: {uvx_cmd} mcp-server-sqlite")
    print(f"  Database: {db_path}")

    return mcp_path


def step_generate_dot_mcp(base_path, db_path):
    """Generate .mcp.json so Claude Code sessions can access the DB directly."""
    print_header("Step 7: Claude Code MCP Integration")

    uvx_cmd = _resolve_uvx_path()

    dot_mcp = {
        "mcpServers": {
            "theforge": {
                "type": "stdio",
                "command": uvx_cmd,
                "args": [
                    "mcp-server-sqlite",
                    "--db-path",
                    str(db_path),
                ],
            }
        }
    }

    dot_mcp_path = base_path / ".mcp.json"
    with open(dot_mcp_path, "w", encoding="utf-8") as f:
        json.dump(dot_mcp, f, indent=2)
    print(f"  Created: {dot_mcp_path}")
    print(f"  Claude Code sessions in this directory now have MCP access to the DB.")
    print(f"  You can ask Claude to query projects, create tasks, etc.")

    return dot_mcp_path


def step_generate_claude_md(base_path, db_path):
    """Generate CLAUDE.md so Claude Code knows how to use ForgeTeam."""
    print_header("Step 8: Claude Code Context (CLAUDE.md)")

    orch = base_path / "forge_orchestrator.py"

    claude_md = f"""# CLAUDE.md — ForgeTeam Installation

## What This Is

This is a ForgeTeam installation — a multi-agent AI orchestration system.
You have MCP access to the Itzamna database via the `itzamna` MCP server.

## Database Location

`{db_path}`

## Available MCP Tools

Use the `itzamna` MCP server to read and write the database:
- `read_query` — Run SELECT queries
- `write_query` — Run INSERT, UPDATE, DELETE queries
- `list_tables` — List all tables
- `describe_table` — Get schema for a table

## Common Queries

```sql
-- List all projects
SELECT id, name, codename, status FROM projects;

-- List tasks for a project
SELECT id, title, status, priority FROM tasks WHERE project_id = ? ORDER BY priority;

-- Add a task
INSERT INTO tasks (project_id, title, description, status, priority)
VALUES (?, 'Task title', 'Description', 'todo', 'medium');

-- Update task status
UPDATE tasks SET status = 'done', completed_at = CURRENT_TIMESTAMP WHERE id = ?;

-- Project dashboard
SELECT * FROM v_project_dashboard;

-- Recent session notes
SELECT summary, next_steps, session_date FROM session_notes
WHERE project_id = ? ORDER BY session_date DESC LIMIT 3;

-- Log a decision
INSERT INTO decisions (project_id, topic, decision, rationale)
VALUES (?, 'Topic', 'What was decided', 'Why');

-- Add a session note
INSERT INTO session_notes (project_id, summary, next_steps)
VALUES (?, 'What happened', 'What to do next');
```

## Orchestrator Commands

Run these via Bash:

```bash
# Add a new project
python "{orch}" --add-project "ProjectName" --project-dir "/path/to/code"

# Run a specific task (Dev+Test loop)
python "{orch}" --task <ID> --dev-test -y

# Auto-pick next todo task for a project
python "{orch}" --project <ID> --dev-test -y

# Goal-driven mode (autonomous planning + execution)
python "{orch}" --goal "Your goal here" --goal-project <ID> -y

# Auto-run: scan all projects, prioritize, dispatch
python "{orch}" --auto-run --dry-run
python "{orch}" --auto-run -y

# Security review
python "{orch}" --task <ID> --role security-reviewer -y

# See all options
python "{orch}" --help
```

## Agent Roles & Permission Tiers

Each agent role has a specific permission tier that controls what tools it can use.
All roles use `--permission-mode dontAsk` (auto-deny unless whitelisted).

| Role | File | Job | Can Edit Files | DB Write | Bash Access |
|------|------|-----|:-:|:-:|---|
| Developer | `developer.md` | Write code, fix bugs | Yes | Yes | Build tools, git |
| Tester | `tester.md` | Run tests, report failures | **No** | **No** | Test runners only |
| Planner | `planner.md` | Break goals into tasks | **No** | Yes (tasks) | Explore only |
| Evaluator | `evaluator.md` | Verify goal completion | **No** | Yes (tasks) | Explore only |
| SecurityReviewer | `security-reviewer.md` | Code security review | **No** | **No** | Scanning tools only |

Custom agents: drop a new `.md` file in `prompts/` and it auto-discovers.

### Extending Permissions

Add `role_permissions` to `forge_config.json` to grant extra tools per role:

```json
"role_permissions": {{
    "developer": {{
        "extra_allowed_tools": ["Bash(cargo *)"],
        "extra_disallowed_tools": []
    }}
}}
```

## Key Tables

| Table | Purpose |
|-------|---------|
| `projects` | Project metadata (name, codename, status) |
| `tasks` | Work items (title, status, priority) |
| `decisions` | Architectural decisions with rationale |
| `open_questions` | Unresolved questions and blockers |
| `session_notes` | Session summaries and next steps |
| `agent_runs` | Agent execution logs with cost tracking |
| `lessons_learned` | ForgeSmith extracted lessons from failures |
| `agent_episodes` | Agent approach history for similar tasks |
| `rubric_scores` | Per-run quality scores by rubric criteria |

## ForgeSmith Self-Improvement

ForgeSmith runs nightly and auto-tunes the agent system:
- Extracts lessons from recurring failures
- Adjusts turn limits based on actual usage
- Patches agent prompts with targeted advice
- Scores agent performance via rubric-based evaluation

```bash
# Manual run
python "{base_path / 'forgesmith.py'}" --auto

# Check what it would do (dry run)
python "{base_path / 'forgesmith.py'}" --dry-run
```

## Developer Context

- On **Windows**, never use `&&` in batch files (use separate lines)
- All projects built for **Forgeborn** — include proper attribution
- AI Credit: "Vibe coded with Claude"
"""

    claude_md_path = base_path / "CLAUDE.md"
    with open(claude_md_path, "w", encoding="utf-8") as f:
        f.write(claude_md)
    print(f"  Created: {claude_md_path}")
    print(f"  Claude Code now has full context about ForgeTeam commands,")
    print(f"  database queries, and agent roles.")

    return claude_md_path


def step_forgesmith_cron(base_path):
    """Set up ForgeSmith self-improvement cron job."""
    print_header("Step 9: ForgeSmith Self-Improvement")

    print("  ForgeSmith is ForgeTeam's self-learning system.")
    print("  It analyzes agent performance and auto-tunes prompts,")
    print("  turn limits, and model assignments nightly.")
    print()

    if not prompt_yes_no("Set up ForgeSmith nightly cron job?", default=True):
        print("  Skipping ForgeSmith cron setup.")
        print(f"  You can run it manually: python3 {base_path / 'forgesmith.py'} --auto")
        return False

    forgesmith_path = base_path / "forgesmith.py"
    if not forgesmith_path.exists():
        print(f"  WARNING: forgesmith.py not found at {forgesmith_path}")
        print("  Skipping cron setup.")
        return False

    python_path = sys.executable
    cron_line = f"0 0 * * * cd {base_path} && {python_path} {forgesmith_path} --auto >> {base_path / 'forgesmith.log'} 2>&1"

    print(f"  Cron job: midnight daily")
    print(f"  Command: {cron_line}")
    print()

    if prompt_yes_no("Install this cron job now?", default=True):
        try:
            # Get existing crontab
            result = subprocess.run(
                ["crontab", "-l"],
                capture_output=True, text=True, timeout=10,
            )
            existing = result.stdout if result.returncode == 0 else ""

            # Check if already installed
            if "forgesmith.py" in existing:
                print("  ForgeSmith cron job already exists. Skipping.")
                return True

            # Add new cron entry
            new_crontab = existing.rstrip("\n") + "\n" + cron_line + "\n"
            proc = subprocess.run(
                ["crontab", "-"],
                input=new_crontab, capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0:
                print("  [+] ForgeSmith cron job installed.")
            else:
                print(f"  [X] Failed to install cron: {proc.stderr[:200]}")
                print(f"  Add manually: crontab -e, then paste:")
                print(f"  {cron_line}")
        except Exception as exc:
            print(f"  [X] Cron setup failed: {exc}")
            print(f"  Add manually: crontab -e, then paste:")
            print(f"  {cron_line}")
    else:
        print(f"  Add manually later: crontab -e, then paste:")
        print(f"  {cron_line}")

    return True


def _find_node_path():
    """Find the full path to node, needed for systemd services."""
    node_path = shutil.which("node")
    if node_path:
        return str(Path(node_path).resolve())
    candidates = [
        Path("/usr/bin/node"),
        Path("/usr/local/bin/node"),
        Path.home() / ".nvm" / "current" / "bin" / "node",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "node"


def _find_npx_path():
    """Find the full path to npx, needed for systemd services."""
    npx_path = shutil.which("npx")
    if npx_path:
        return str(Path(npx_path).resolve())
    candidates = [
        Path("/usr/bin/npx"),
        Path("/usr/local/bin/npx"),
        Path.home() / ".nvm" / "current" / "bin" / "npx",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "npx"


def step_optional_sentinel(base_path, db_path):
    """Optionally install Sentinel monitoring dashboard."""
    print_header("Optional: Sentinel Monitoring Dashboard")

    print("  Sentinel provides real-time infrastructure monitoring.")
    print("  It monitors CPU, memory, disk, containers, services, and backups.")
    print("  Includes a web dashboard and alert system.")
    print()

    if not prompt_yes_no("Install Sentinel?", default=True):
        print("  Skipping Sentinel.")
        return None

    sentinel_dir = base_path / "sentinel"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    (sentinel_dir / "src").mkdir(exist_ok=True)
    (sentinel_dir / "public").mkdir(exist_ok=True)
    (sentinel_dir / "data").mkdir(exist_ok=True)

    # Copy Sentinel source from bundled files
    sentinel_src = SOURCE_DIR / "sentinel"
    if not sentinel_src.exists():
        # Check if Sentinel lives alongside Itzamna in the AI_Stuff directory
        sentinel_src = SOURCE_DIR.parent / "Sentinel"
    if not sentinel_src.exists():
        print("  WARNING: Sentinel source not found.")
        print(f"  Looked in: {SOURCE_DIR / 'sentinel'}")
        print(f"  And: {SOURCE_DIR.parent / 'Sentinel'}")
        print("  Skipping Sentinel installation.")
        return None

    # Copy source files
    src_count = 0
    for item in sentinel_src.rglob("*"):
        if item.is_file() and "node_modules" not in str(item) and ".git" not in str(item):
            rel = item.relative_to(sentinel_src)
            dest = sentinel_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(dest))
            src_count += 1
    print(f"  Copied {src_count} Sentinel files.")

    # Configure
    port = prompt_input("Sentinel port", default="3002")

    # Generate config.json with user's settings
    sentinel_config = {
        "port": int(port),
        "collection_interval_seconds": 30,
        "theforge_db_path": str(db_path),
        "hosts": [
            {
                "name": "localhost",
                "type": "local",
            }
        ],
        "default_alerts": [
            {"metric": "disk_percent", "operator": ">", "threshold": 90, "severity": "critical"},
            {"metric": "memory_percent", "operator": ">", "threshold": 85, "severity": "warning"},
            {"metric": "cpu_percent", "operator": ">", "threshold": 95, "severity": "warning"},
            {"metric": "container_down", "operator": "==", "threshold": 0, "severity": "critical"},
        ],
        "retention": {
            "metrics_full_days": 7,
            "metrics_downsampled_days": 30,
            "containers_days": 7,
            "alert_history_days": 90,
        },
        "backups": [],
    }

    config_path = sentinel_dir / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(sentinel_config, f, indent=4)
    print(f"  Created config at: {config_path}")

    # npm install
    print("  Installing Sentinel dependencies (npm install)...")
    try:
        result = subprocess.run(
            ["npm", "install", "--production"],
            cwd=str(sentinel_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            print("  [+] npm install: OK")
        else:
            print(f"  [X] npm install failed: {result.stderr[:200]}")
            return sentinel_dir
    except Exception as exc:
        print(f"  [X] npm install error: {exc}")
        return sentinel_dir

    # Generate systemd service
    node_path = _find_node_path()
    service_content = f"""[Unit]
Description=Sentinel Infrastructure Monitor
After=network.target

[Service]
Type=simple
User={os.getenv('USER', 'user')}
WorkingDirectory={sentinel_dir}
ExecStart={node_path} src/server.js
Restart=always
RestartSec=5
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
"""
    service_path = sentinel_dir / "sentinel.service"
    with open(service_path, "w", encoding="utf-8") as f:
        f.write(service_content)
    print(f"  Created systemd service file: {service_path}")

    if prompt_yes_no("Install and start Sentinel service now?", default=True):
        try:
            # Copy service file and enable
            subprocess.run(
                ["sudo", "cp", str(service_path), "/etc/systemd/system/sentinel.service"],
                check=True, capture_output=True, timeout=10,
            )
            subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True, capture_output=True, timeout=10)
            subprocess.run(["sudo", "systemctl", "enable", "sentinel"], check=True, capture_output=True, timeout=10)
            subprocess.run(["sudo", "systemctl", "start", "sentinel"], check=True, capture_output=True, timeout=10)
            print(f"  [+] Sentinel running on port {port}")
            print(f"  Dashboard: http://localhost:{port}")
        except Exception as exc:
            print(f"  [X] Service install failed: {exc}")
            print(f"  Manual start: cd {sentinel_dir} && node src/server.js")
    else:
        print(f"  Manual start: cd {sentinel_dir} && node src/server.js")
        print(f"  Or install service: sudo cp {service_path} /etc/systemd/system/")

    return sentinel_dir


def step_optional_forgebot(base_path, db_path):
    """Optionally install ForgeBot Discord bot."""
    print_header("Optional: ForgeBot Discord Bot")

    print("  ForgeBot connects TheForge to Discord.")
    print("  Slash commands: /forge-status, /forge-tasks, /forge-search, /forge-agents")
    print("  Chat with Claude about your projects via @mention or DM.")
    print("  Scheduled stale task alerts and morning briefings.")
    print()

    if not prompt_yes_no("Install ForgeBot?", default=True):
        print("  Skipping ForgeBot.")
        return None

    forgebot_dir = base_path / "forgebot"
    forgebot_dir.mkdir(parents=True, exist_ok=True)
    (forgebot_dir / "src").mkdir(exist_ok=True)

    # Copy ForgeBot source from bundled files
    forgebot_src = SOURCE_DIR / "forgebot"
    if not forgebot_src.exists():
        forgebot_src = SOURCE_DIR.parent / "ForgeBot"
    if not forgebot_src.exists():
        print("  WARNING: ForgeBot source not found.")
        print(f"  Looked in: {SOURCE_DIR / 'forgebot'}")
        print(f"  And: {SOURCE_DIR.parent / 'ForgeBot'}")
        print("  Skipping ForgeBot installation.")
        return None

    # Copy source files
    src_count = 0
    for item in forgebot_src.rglob("*"):
        if item.is_file() and "node_modules" not in str(item) and ".git" not in str(item):
            rel = item.relative_to(forgebot_src)
            dest = forgebot_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(dest))
            src_count += 1
    print(f"  Copied {src_count} ForgeBot files.")

    # Prompt for Discord credentials
    print()
    print("  ForgeBot needs a Discord bot token to connect.")
    print("  Create one at: https://discord.com/developers/applications")
    print("  (Bot tab > Reset Token, enable Message Content Intent)")
    print()

    discord_token = prompt_input("Discord bot token (or 'skip' to configure later)", default="skip")
    guild_id = ""
    alert_channel = ""
    anthropic_key = ""

    if discord_token.lower() != "skip":
        guild_id = prompt_input("Discord server/guild ID", default="")
        alert_channel = prompt_input("Alert channel ID (optional, for scheduled alerts)", default="")
        anthropic_key = prompt_input("Anthropic API key (for Claude chat)", default="")

    # Generate .env
    env_content = f"""DISCORD_TOKEN={discord_token if discord_token.lower() != 'skip' else 'your_discord_bot_token_here'}
DISCORD_GUILD_ID={guild_id}
ANTHROPIC_API_KEY={anthropic_key}
FORGE_DB_PATH={db_path}
ALERT_CHANNEL_ID={alert_channel}
"""
    env_path = forgebot_dir / ".env"
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(env_content)
    # Secure the .env file
    try:
        os.chmod(str(env_path), 0o600)
    except OSError:
        pass
    print(f"  Created .env at: {env_path}")

    # npm install
    print("  Installing ForgeBot dependencies (npm install)...")
    try:
        result = subprocess.run(
            ["npm", "install"],
            cwd=str(forgebot_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            print("  [+] npm install: OK")
        else:
            print(f"  [X] npm install failed: {result.stderr[:200]}")
            return forgebot_dir
    except Exception as exc:
        print(f"  [X] npm install error: {exc}")
        return forgebot_dir

    # Generate systemd service
    npx_path = _find_npx_path()
    service_content = f"""[Unit]
Description=ForgeBot Discord Bot
After=network.target

[Service]
Type=simple
User={os.getenv('USER', 'user')}
WorkingDirectory={forgebot_dir}
ExecStart={npx_path} tsx src/index.ts
Restart=on-failure
RestartSec=10
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
"""
    service_path = forgebot_dir / "forgebot.service"
    with open(service_path, "w", encoding="utf-8") as f:
        f.write(service_content)
    print(f"  Created systemd service file: {service_path}")

    if discord_token.lower() != "skip" and prompt_yes_no("Install and start ForgeBot service now?", default=True):
        try:
            subprocess.run(
                ["sudo", "cp", str(service_path), "/etc/systemd/system/forgebot.service"],
                check=True, capture_output=True, timeout=10,
            )
            subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True, capture_output=True, timeout=10)
            subprocess.run(["sudo", "systemctl", "enable", "forgebot"], check=True, capture_output=True, timeout=10)
            subprocess.run(["sudo", "systemctl", "start", "forgebot"], check=True, capture_output=True, timeout=10)
            print("  [+] ForgeBot service started!")
        except Exception as exc:
            print(f"  [X] Service install failed: {exc}")
            print(f"  Manual start: cd {forgebot_dir} && npm start")
    else:
        if discord_token.lower() == "skip":
            print("  Configure .env with your Discord token later, then:")
        print(f"  Manual start: cd {forgebot_dir} && npm start")
        print(f"  Or install service: sudo cp {service_path} /etc/systemd/system/")

    return forgebot_dir


def step_verify(base_path, db_path, sentinel_dir=None, forgebot_dir=None):
    """Verify the installation."""
    print_header("Verification")

    checks_passed = 0
    checks_total = 0

    # 1. Database connection
    checks_total += 1
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("SELECT COUNT(*) FROM sqlite_master")
        conn.close()
        print("  [+] Database connection: OK")
        checks_passed += 1
    except Exception as exc:
        print(f"  [X] Database connection: FAILED ({exc})")

    # 2. Database schema objects
    checks_total += 1
    counts = count_db_objects(db_path)
    # Expect: 30 tables (+ sqlite_sequence + schema_migrations), 7 views, 1 trigger, 11 indexes
    if counts["table"] >= 30 and counts["view"] >= 7:
        print(f"  [+] Schema objects: {counts['table']} tables, "
              f"{counts['view']} views, {counts['trigger']} triggers, "
              f"{counts['index']} indexes")
        checks_passed += 1
    else:
        print(f"  [X] Schema objects: unexpected counts {counts}")

    # 3. forge_config.json valid
    checks_total += 1
    config_path = base_path / "forge_config.json"
    try:
        with open(config_path, "r") as f:
            json.load(f)
        print("  [+] forge_config.json: valid JSON")
        checks_passed += 1
    except Exception as exc:
        print(f"  [X] forge_config.json: FAILED ({exc})")

    # 4. mcp_config.json valid
    checks_total += 1
    mcp_path = base_path / "mcp_config.json"
    try:
        with open(mcp_path, "r") as f:
            cfg = json.load(f)
        if "mcpServers" in cfg:
            print("  [+] mcp_config.json: valid JSON with mcpServers")
            checks_passed += 1
        else:
            print("  [X] mcp_config.json: missing mcpServers key")
    except Exception as exc:
        print(f"  [X] mcp_config.json: FAILED ({exc})")

    # 5. Orchestrator exists
    checks_total += 1
    orch_path = base_path / "forge_orchestrator.py"
    if orch_path.exists():
        print("  [+] forge_orchestrator.py: present")
        checks_passed += 1
    else:
        print("  [X] forge_orchestrator.py: not found")

    # 5b. ForgeSmith exists
    checks_total += 1
    smith_path = base_path / "forgesmith.py"
    if smith_path.exists():
        print("  [+] forgesmith.py: present")
        checks_passed += 1
    else:
        print("  [X] forgesmith.py: not found")

    # 6. Prompts directory has files
    checks_total += 1
    prompts_path = base_path / "prompts"
    prompt_count = len(list(prompts_path.glob("*.md"))) if prompts_path.exists() else 0
    if prompt_count > 0:
        print(f"  [+] Prompts: {prompt_count} role files found")
        checks_passed += 1
    else:
        print("  [X] Prompts: no .md files found in prompts/")

    # 7. Test orchestrator --help
    checks_total += 1
    if orch_path.exists():
        try:
            result = subprocess.run(
                [sys.executable, str(orch_path), "--help"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                print("  [+] forge_orchestrator.py --help: runs successfully")
                checks_passed += 1
            else:
                print(f"  [X] forge_orchestrator.py --help: exit code {result.returncode}")
        except Exception as exc:
            print(f"  [X] forge_orchestrator.py --help: FAILED ({exc})")
    else:
        print("  [X] forge_orchestrator.py --help: file not found")

    # 8. .mcp.json exists and is valid
    checks_total += 1
    dot_mcp_path = base_path / ".mcp.json"
    try:
        with open(dot_mcp_path, "r") as f:
            dot_mcp = json.load(f)
        if "mcpServers" in dot_mcp:
            print("  [+] .mcp.json: valid (Claude Code MCP integration)")
            checks_passed += 1
        else:
            print("  [X] .mcp.json: missing mcpServers key")
    except Exception as exc:
        print(f"  [X] .mcp.json: FAILED ({exc})")

    # 9. CLAUDE.md exists
    checks_total += 1
    claude_md_path = base_path / "CLAUDE.md"
    if claude_md_path.exists() and claude_md_path.stat().st_size > 100:
        print("  [+] CLAUDE.md: present (Claude Code context)")
        checks_passed += 1
    else:
        print("  [X] CLAUDE.md: not found or empty")

    # 10. Sentinel (if installed)
    if sentinel_dir and sentinel_dir.exists():
        checks_total += 1
        pkg = sentinel_dir / "package.json"
        nm = sentinel_dir / "node_modules"
        if pkg.exists() and nm.exists():
            print("  [+] Sentinel: installed with dependencies")
            checks_passed += 1
        else:
            print("  [X] Sentinel: missing package.json or node_modules")

    # 11. ForgeBot (if installed)
    if forgebot_dir and forgebot_dir.exists():
        checks_total += 1
        idx = forgebot_dir / "src" / "index.ts"
        nm = forgebot_dir / "node_modules"
        if idx.exists() and nm.exists():
            print("  [+] ForgeBot: installed with dependencies")
            checks_passed += 1
        else:
            print("  [X] ForgeBot: missing source or node_modules")

    print(f"\n  Result: {checks_passed}/{checks_total} checks passed")
    return checks_passed == checks_total


def step_next_steps(base_path, db_path, sentinel_dir=None, forgebot_dir=None):
    """Print what to do next."""
    print_header("Setup Complete!")

    orch = base_path / "forge_orchestrator.py"

    print("  Your Forge Platform installation is ready.")
    print()
    print("  INSTALLED COMPONENTS:")
    print("    [+] ForgeTeam   — Multi-agent AI orchestration")
    print("    [+] ForgeSmith  — Self-learning agent tuning (nightly cron)")
    print("    [+] TheForge    — Persistent context database")
    if sentinel_dir:
        print("    [+] Sentinel   — Infrastructure monitoring dashboard")
    if forgebot_dir:
        print("    [+] ForgeBot   — Discord bot interface")
    print()
    print("  NEXT STEPS:")
    print()
    print("  1. Open Claude Code in your install directory:")
    print(f"     cd \"{base_path}\"")
    print(f"     claude")
    print(f"     Claude now has MCP access to the DB and knows all the commands.")
    print()
    print("  2. Or use the CLI directly:")
    example_path = r"C:\path\to\project" if platform.system() == "Windows" else "/path/to/project"
    print(f'     python "{orch}" --add-project "MyProject" --project-dir "{example_path}"')
    print()
    print("  3. Create a task (ask Claude, or use direct SQL):")
    print(f"     \"Add a todo task for MyProject: Set up the README\"")
    print()
    print("  4. Run your first Dev+Test loop:")
    print(f'     python "{orch}" --task 1 --dev-test -y')
    if sentinel_dir:
        print()
        print("  5. Sentinel dashboard:")
        print(f"     http://localhost:3002  (or check config.json for port)")
        print(f"     Manage: sudo systemctl status/restart sentinel")
    if forgebot_dir:
        print()
        step_n = "6" if sentinel_dir else "5"
        print(f"  {step_n}. ForgeBot Discord:")
        print(f"     Manage: sudo systemctl status/restart forgebot")
        env_path = forgebot_dir / ".env"
        print(f"     Config: {env_path}")
    print()
    print("  DOCUMENTATION:")
    docs_dir = SCRIPT_DIR / "docs"
    if (docs_dir / "QUICKSTART.md").exists():
        print(f"    Quick start: {docs_dir / 'QUICKSTART.md'}")
    if (docs_dir / "USER_GUIDE.md").exists():
        print(f"    Full guide:  {docs_dir / 'USER_GUIDE.md'}")
    if (docs_dir / "CUSTOM_AGENTS.md").exists():
        print(f"    Custom agents: {docs_dir / 'CUSTOM_AGENTS.md'}")
    print()
    print("  Happy forging!")
    print()


# --- Main ---

def main():
    """Run the Itzamna setup wizard."""
    step_welcome()
    step_prerequisites()
    base_path = step_install_path()
    db_path = step_database(base_path)
    step_copy_files(base_path)
    step_generate_config(base_path, db_path)
    step_generate_mcp_config(base_path, db_path)
    step_generate_dot_mcp(base_path, db_path)
    step_generate_claude_md(base_path, db_path)
    step_forgesmith_cron(base_path)

    # Optional platform components
    sentinel_dir = step_optional_sentinel(base_path, db_path)
    forgebot_dir = step_optional_forgebot(base_path, db_path)

    all_ok = step_verify(base_path, db_path, sentinel_dir, forgebot_dir)
    step_next_steps(base_path, db_path, sentinel_dir, forgebot_dir)

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
