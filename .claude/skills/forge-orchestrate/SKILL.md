# /forge-orchestrate

Launch EQUIPA orchestrator agents to work on tasks autonomously.

## What This Skill Does

1. Resolves the project and tasks from the database
2. Launches orchestrator process(es) locally
3. Reports status and provides monitoring commands

## Usage

```
/forge-orchestrate MyProject 42-44          # Run specific tasks for a project
/forge-orchestrate 3 42                     # By project ID, single task
/forge-orchestrate MyProject --auto         # Auto-run all pending tasks for project
/forge-orchestrate MyProject 50 --model opus  # Override model
/forge-orchestrate --status                 # Check running orchestrators
/forge-orchestrate --kill                   # Kill all running orchestrators
```

## Instructions

When this skill is invoked:

### Step 0: Find the orchestrator

Look for `forge_orchestrator.py` in these locations (in order):
1. Current working directory
2. Parent directory
3. The directory where `theforge.db` is located (check MCP server config)

If not found, ask the user for the path.

### Step 0a: If --status flag, just check status

```bash
ps aux | grep forge_orchestrator | grep -v grep
```

Show running orchestrators with their task IDs and PIDs. Done.

### Step 0b: If --kill flag, kill orchestrators

**IMPORTANT:** Always confirm with the user before killing orchestrators. Show what's running first.

```bash
pkill -f forge_orchestrator && echo 'All orchestrators killed' || echo 'No orchestrators running'
```

### Step 1: Resolve the project

```sql
-- Find project by name or ID
SELECT id, name, codename, status FROM projects
WHERE codename LIKE '%{arg}%' OR name LIKE '%{arg}%' OR id = CAST('{arg}' AS INTEGER)
LIMIT 1;
```

### Step 2: Validate the tasks

If specific task IDs provided:

```sql
SELECT id, title, status, priority, complexity FROM tasks
WHERE id IN ({task_ids}) AND project_id = {project_id};
```

Verify:
- All tasks belong to the specified project
- Tasks are not already `done`
- Tasks exist in the database

If `--auto` flag, find all pending tasks:

```sql
SELECT id, title, status, priority, complexity FROM tasks
WHERE project_id = {project_id} AND status IN ('todo', 'in_progress')
ORDER BY priority DESC, id;
```

### Step 3: Launch the orchestrator

For specific tasks (e.g., `42-44`), use the `--tasks` flag for same-project parallel execution:

```bash
nohup python3 -u {orchestrator_path}/forge_orchestrator.py --tasks {start}-{end} --dev-test -y > /tmp/forge-tasks-{start}-{end}.log 2>&1 &
```

For a single task:

```bash
nohup python3 -u {orchestrator_path}/forge_orchestrator.py --task {task_id} --dev-test -y > /tmp/forge-task-{task_id}.log 2>&1 &
```

For auto-run:

```bash
nohup python3 -u {orchestrator_path}/forge_orchestrator.py --auto-run -y > /tmp/forge-autorun-{project_id}.log 2>&1 &
```

**NOTE:** If tasks span multiple projects, launch separate orchestrator processes per project (--tasks requires all tasks from the same project).

### Step 4: Verify launch and report

After launching, verify the processes are running:

```bash
ps aux | grep forge_orchestrator | grep -v grep
```

Report to the user:
```
Orchestrator launched for {project_name}:
- Tasks {ids}: {titles} (PID: {pid})

Monitor logs:
  tail -f /tmp/forge-task-{id}.log

Check status:
  /forge-orchestrate --status
```

### Step 5: Update task status

```sql
UPDATE tasks SET status = 'in_progress' WHERE id IN ({task_ids}) AND status = 'todo';
```

## Model Override Options

| Flag | Effect |
|------|--------|
| `--model opus` | Set all developer/debugger/planner roles to opus |
| `--model sonnet` | Set all roles to sonnet (faster, cheaper) |
| `--fast` | sonnet developers, haiku testers (cheapest) |
| `--quality` | opus everything (most expensive, best results) |

## Safety Notes

- **Always show the user what will be launched before launching.** List the tasks and models.
- **Never kill running orchestrators without user confirmation.**
- **Orchestrators manage their own task status** — don't manually update task status to 'done' unless you verified the orchestrator completed successfully.
- **Log files** are at `/tmp/forge-task-{id}.log` and `/tmp/forge-autorun-{project_id}.log`.
