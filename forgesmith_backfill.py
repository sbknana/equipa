#!/usr/bin/env python3
"""Backfill agent_runs from historical orchestrator log files.

Parses the PROJECT POMBAL AGENT RUN SUMMARY blocks and [DB] Task lines
from all orchestrator logs to populate the agent_runs table.

Usage:
    python3 forgesmith_backfill.py
"""

import glob
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

THEFORGE_DB = os.environ.get(
    "THEFORGE_DB",
    str(Path(__file__).resolve().parent / "theforge.db"),
)

# All known log locations
LOG_PATTERNS = [
    "/tmp/*-orch*.log",
    "/tmp/forge-orchestrator-*.log",
    "forge_orchestrator_*.log",
]


def get_db():
    conn = sqlite3.connect(THEFORGE_DB)
    conn.row_factory = sqlite3.Row
    return conn


def get_task_info(conn, task_id):
    """Look up task metadata from TheForge."""
    row = conn.execute(
        """SELECT t.id, t.project_id, t.complexity, t.role, t.status,
                  p.codename as project_name
           FROM tasks t
           LEFT JOIN projects p ON t.project_id = p.id
           WHERE t.id = ?""",
        (task_id,),
    ).fetchone()
    return dict(row) if row else None


def parse_summary_blocks(content):
    """Parse PROJECT POMBAL AGENT RUN SUMMARY blocks from log content.

    Returns list of dicts with task_id, turns, duration, status.
    """
    runs = []

    # Pattern for summary blocks
    # Task:      #434 - Scaffold Next.js 14 project...
    # Status:    SUCCESS or FAILED or BLOCKED
    # Turns:     74
    # Duration:  502.8s
    summary_pattern = re.compile(
        r'Task:\s+#(\d+)\s*-\s*(.+?)\n'
        r'.*?Project:\s*(\w+).*?\n'
        r'.*?Status:\s+(\w+)\s*\n'
        r'.*?Turns:\s+(\d+)\s*\n'
        r'.*?Duration:\s+([\d.]+)s',
        re.DOTALL,
    )

    # Also try simpler pattern without Project line
    simple_pattern = re.compile(
        r'Task:\s+#(\d+)\s*-\s*(.+?)\n'
        r'.*?Status:\s+(\w+)\s*\n'
        r'.*?Turns:\s+(\d+)\s*\n'
        r'.*?Duration:\s+([\d.]+)s',
        re.DOTALL,
    )

    for match in summary_pattern.finditer(content):
        task_id = int(match.group(1))
        title = match.group(2).strip()
        project = match.group(3).strip()
        status = match.group(4).strip()
        turns = int(match.group(5))
        duration = float(match.group(6))

        runs.append({
            "task_id": task_id,
            "title": title,
            "project_name": project,
            "status": status,
            "turns": turns,
            "duration": duration,
        })

    # If no full matches, try simple pattern
    if not runs:
        for match in simple_pattern.finditer(content):
            task_id = int(match.group(1))
            title = match.group(2).strip()
            status = match.group(3).strip()
            turns = int(match.group(4))
            duration = float(match.group(5))

            runs.append({
                "task_id": task_id,
                "title": title,
                "project_name": None,
                "status": status,
                "turns": turns,
                "duration": duration,
            })

    return runs


def parse_db_updates(content):
    """Parse [DB] Task status updates and nearby context.

    Returns list of dicts with task_id, old_status, new_status, outcome.
    """
    updates = []
    # [DB] Task 434: todo -> done (outcome: tests_passed)
    pattern = re.compile(
        r'\[DB\] Task (\d+): (\w+) -> (\w+) \(outcome: ([\w_]+)\)'
    )
    for match in pattern.finditer(content):
        updates.append({
            "task_id": int(match.group(1)),
            "old_status": match.group(2),
            "new_status": match.group(3),
            "outcome": match.group(4),
        })
    return updates


def parse_cost_data(content):
    """Extract costUSD from JSON stats blocks in logs.

    Returns dict mapping approximate position to cost value.
    """
    costs = {}
    # Find costUSD values near task boundaries
    cost_pattern = re.compile(r'"costUSD":([\d.]+)')
    task_pattern = re.compile(r'Task\s+#(\d+)')

    # Strategy: find all costUSD values and associate with nearest preceding task
    lines = content.split('\n')
    current_task_id = None
    for line in lines:
        task_match = task_pattern.search(line)
        if task_match:
            current_task_id = int(task_match.group(1))

        cost_matches = cost_pattern.findall(line)
        if cost_matches and current_task_id:
            # Sum all model costs in this line
            total_cost = sum(float(c) for c in cost_matches)
            if current_task_id not in costs or total_cost > costs[current_task_id]:
                costs[current_task_id] = total_cost

    return costs


def parse_max_turns_info(content):
    """Find which tasks hit max turns."""
    hits = set()
    # Patterns: "Agent hit max turns limit", "Developer hit max turns"
    lines = content.split('\n')
    current_task_id = None
    for line in lines:
        task_match = re.search(r'Task\s+#?(\d+)', line)
        if task_match:
            current_task_id = int(task_match.group(1))
        if 'hit max turns' in line.lower() and current_task_id:
            hits.add(current_task_id)
    return hits


def parse_model_info(content):
    """Extract model assignments from log lines."""
    models = {}
    lines = content.split('\n')
    current_task_id = None
    for line in lines:
        task_match = re.search(r'Task\s+#?(\d+)', line)
        if task_match:
            current_task_id = int(task_match.group(1))

        model_match = re.search(r'Model:\s+(\w+)', line)
        if model_match and current_task_id:
            models[current_task_id] = model_match.group(1)

        # Also check Developer: model=opus patterns
        dev_model_match = re.search(r'Developer: model=(\w+)', line)
        if dev_model_match and current_task_id:
            models[current_task_id] = dev_model_match.group(1)

    return models


def parse_max_turns_values(content):
    """Extract max_turns settings from log lines."""
    max_turns = {}
    lines = content.split('\n')
    current_task_id = None
    for line in lines:
        task_match = re.search(r'Task\s+#?(\d+)', line)
        if task_match:
            current_task_id = int(task_match.group(1))

        turns_match = re.search(r'Max turns:\s+(\d+)', line)
        if turns_match and current_task_id:
            max_turns[current_task_id] = int(turns_match.group(1))

        dev_turns_match = re.search(r'Developer:.*max_turns=(\d+)', line)
        if dev_turns_match and current_task_id:
            max_turns[current_task_id] = int(dev_turns_match.group(1))

    return max_turns


def parse_complexity(content):
    """Extract complexity from log lines."""
    complexities = {}
    lines = content.split('\n')
    current_task_id = None
    for line in lines:
        task_match = re.search(r'Task\s+#?(\d+)', line)
        if task_match:
            current_task_id = int(task_match.group(1))

        # Complexity: complex
        comp_match = re.search(r'Complexity:\s+(\w+)', line)
        if comp_match and current_task_id:
            complexities[current_task_id] = comp_match.group(1)

        # Task complexity: complex
        comp_match2 = re.search(r'Task complexity:\s+(\w+)', line)
        if comp_match2 and current_task_id:
            complexities[current_task_id] = comp_match2.group(1)

    return complexities


def get_log_timestamp(log_path):
    """Get approximate timestamp from log file modification time."""
    stat = os.stat(log_path)
    return datetime.fromtimestamp(stat.st_mtime)


def backfill():
    """Main backfill routine."""
    # Collect all log files
    log_files = []
    for pattern in LOG_PATTERNS:
        log_files.extend(glob.glob(pattern))

    # Deduplicate
    log_files = sorted(set(log_files))
    print(f"Found {len(log_files)} log files to parse")

    conn = get_db()

    # Check existing runs to avoid duplicates
    existing = set()
    for row in conn.execute("SELECT task_id FROM agent_runs"):
        existing.add(row["task_id"])

    total_inserted = 0
    total_skipped = 0

    for log_path in log_files:
        try:
            with open(log_path, 'r', errors='replace') as f:
                content = f.read()
        except Exception as e:
            print(f"  ERROR reading {log_path}: {e}")
            continue

        if not content.strip():
            continue

        log_time = get_log_timestamp(log_path)

        # Parse all data from this log
        summaries = parse_summary_blocks(content)
        db_updates = parse_db_updates(content)
        costs = parse_cost_data(content)
        max_turns_hits = parse_max_turns_info(content)
        models = parse_model_info(content)
        max_turns_vals = parse_max_turns_values(content)
        complexities = parse_complexity(content)

        # Build a lookup of DB updates by task_id
        outcome_map = {}
        for u in db_updates:
            outcome_map[u["task_id"]] = u["outcome"]

        # Process summaries (most complete data)
        seen_in_log = set()
        for summary in summaries:
            task_id = summary["task_id"]
            if task_id in existing or task_id in seen_in_log:
                total_skipped += 1
                continue
            seen_in_log.add(task_id)

            # Look up task in DB
            task_info = get_task_info(conn, task_id)
            project_id = task_info["project_id"] if task_info else None
            complexity = complexities.get(task_id) or (task_info["complexity"] if task_info else None)
            role = (task_info["role"] if task_info else None) or "developer"

            # Determine outcome
            outcome = outcome_map.get(task_id)
            if not outcome:
                if summary["status"] == "SUCCESS":
                    outcome = "tests_passed"
                elif summary["status"] == "BLOCKED":
                    outcome = "developer_blocked"
                else:
                    outcome = "developer_failed"

            success = 1 if outcome in ("tests_passed", "no_tests") else 0
            model = models.get(task_id, "opus")
            max_turns = max_turns_vals.get(task_id, 50)
            cost = costs.get(task_id)
            hit_max = task_id in max_turns_hits

            error_type = None
            error_summary = None
            if hit_max:
                error_type = "max_turns"
                error_summary = f"Agent hit max turns limit ({max_turns})"

            # Estimate started_at from log file time and duration
            duration = summary["duration"]
            completed_at = log_time.strftime("%Y-%m-%d %H:%M:%S")
            started_at = (log_time - timedelta(seconds=duration)).strftime("%Y-%m-%d %H:%M:%S")

            conn.execute(
                """INSERT INTO agent_runs
                   (task_id, project_id, role, model, complexity, num_turns,
                    max_turns_allowed, duration_seconds, cost_usd, outcome,
                    success, cycle_number, continuation_count, files_changed_count,
                    error_type, error_summary, started_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, ?, ?, ?, ?)""",
                (task_id, project_id, role, model, complexity, summary["turns"],
                 max_turns, duration, cost, outcome,
                 success, error_type, error_summary, started_at, completed_at),
            )
            total_inserted += 1
            existing.add(task_id)

        # Also process DB updates that didn't have summary blocks
        for update in db_updates:
            task_id = update["task_id"]
            if task_id in existing or task_id in seen_in_log:
                continue
            seen_in_log.add(task_id)

            task_info = get_task_info(conn, task_id)
            project_id = task_info["project_id"] if task_info else None
            complexity = complexities.get(task_id) or (task_info["complexity"] if task_info else None)
            role = (task_info["role"] if task_info else None) or "developer"

            outcome = update["outcome"]
            success = 1 if outcome in ("tests_passed", "no_tests") else 0
            model = models.get(task_id, "opus")
            max_turns = max_turns_vals.get(task_id, 50)
            cost = costs.get(task_id)
            hit_max = task_id in max_turns_hits

            error_type = None
            error_summary = None
            if hit_max:
                error_type = "max_turns"
                error_summary = f"Agent hit max turns limit ({max_turns})"

            completed_at = log_time.strftime("%Y-%m-%d %H:%M:%S")

            conn.execute(
                """INSERT INTO agent_runs
                   (task_id, project_id, role, model, complexity, num_turns,
                    max_turns_allowed, duration_seconds, cost_usd, outcome,
                    success, cycle_number, continuation_count, files_changed_count,
                    error_type, error_summary, started_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, 0, ?, ?, ?, 1, 0, 0, ?, ?, ?, ?)""",
                (task_id, project_id, role, model, complexity,
                 max_turns, cost, outcome,
                 success, error_type, error_summary, completed_at, completed_at),
            )
            total_inserted += 1
            existing.add(task_id)

        if seen_in_log:
            print(f"  {log_path}: {len(seen_in_log)} runs extracted")

    conn.commit()
    conn.close()

    print(f"\nBackfill complete: {total_inserted} runs inserted, {total_skipped} duplicates skipped")
    return total_inserted


if __name__ == "__main__":
    backfill()
