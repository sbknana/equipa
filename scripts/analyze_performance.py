#!/usr/bin/env python3
"""
EQUIPA Performance Analysis Script
Copyright 2026, Forgeborn

Parses orchestrator checkpoint files and TheForge task history to produce
a structured JSON report with:
  - Task completion rates by project/priority/complexity
  - Failure and blocked rates
  - Checkpoint/continuation frequency and cost data
  - Common error patterns from checkpoint files
  - Time-to-complete by complexity level
  - Per-model cost and usage breakdown
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


def load_config(script_dir):
    """Load forge_config.json for DB path and other settings."""
    config_path = os.path.join(script_dir, "forge_config.json")
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return json.load(f)
    return {}


def connect_db(config):
    """Connect to TheForge SQLite database."""
    db_path = config.get("theforge_db", "")
    if not db_path or not os.path.exists(db_path):
        print(f"Error: Database not found at '{db_path}'", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Checkpoint parsing
# ---------------------------------------------------------------------------

def parse_checkpoint_filename(filename):
    """Extract task_id, role, and attempt number from checkpoint filename.

    Pattern: task_{id}_{role}_attempt_{n}.txt
    """
    match = re.match(r"task_(\d+)_(\w+)_attempt_(\d+)\.txt", filename)
    if not match:
        return None
    return {
        "task_id": int(match.group(1)),
        "role": match.group(2),
        "attempt": int(match.group(3)),
    }


def parse_checkpoint_file(filepath):
    """Parse a single checkpoint JSON file for metrics."""
    try:
        with open(filepath, "r") as f:
            data = json.loads(f.read().strip())
    except (json.JSONDecodeError, OSError):
        return None

    result = {
        "subtype": data.get("subtype", "unknown"),
        "duration_ms": data.get("duration_ms", 0),
        "duration_api_ms": data.get("duration_api_ms", 0),
        "num_turns": data.get("num_turns", 0),
        "total_cost_usd": data.get("total_cost_usd", 0.0),
        "is_error": data.get("is_error", False),
        "errors": data.get("errors", []),
        "model_usage": {},
    }

    # Per-model breakdown
    for model_id, usage in data.get("modelUsage", {}).items():
        result["model_usage"][model_id] = {
            "input_tokens": usage.get("inputTokens", 0),
            "output_tokens": usage.get("outputTokens", 0),
            "cache_read_tokens": usage.get("cacheReadInputTokens", 0),
            "cache_creation_tokens": usage.get("cacheCreationInputTokens", 0),
            "cost_usd": usage.get("costUSD", 0.0),
        }

    return result


def load_all_checkpoints(checkpoints_dir):
    """Load and aggregate all checkpoint files."""
    checkpoints = []
    if not os.path.isdir(checkpoints_dir):
        return checkpoints

    for filename in os.listdir(checkpoints_dir):
        if not filename.endswith(".txt"):
            continue
        meta = parse_checkpoint_filename(filename)
        if not meta:
            continue
        filepath = os.path.join(checkpoints_dir, filename)
        metrics = parse_checkpoint_file(filepath)
        if metrics:
            checkpoints.append({**meta, **metrics})

    return checkpoints


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

def get_task_stats(conn, project_id=None):
    """Get task counts grouped by status."""
    where = "WHERE t.project_id = ?" if project_id else ""
    params = (project_id,) if project_id else ()
    query = f"""
        SELECT t.status, COUNT(*) as count
        FROM tasks t
        {where}
        GROUP BY t.status
    """
    return {row["status"]: row["count"] for row in conn.execute(query, params)}


def get_completion_rates_by_project(conn):
    """Completion rates per project."""
    query = """
        SELECT p.id, p.name, p.codename, p.status as project_status,
               COUNT(*) as total_tasks,
               SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END) as done,
               SUM(CASE WHEN t.status = 'blocked' THEN 1 ELSE 0 END) as blocked,
               SUM(CASE WHEN t.status = 'todo' THEN 1 ELSE 0 END) as todo,
               SUM(CASE WHEN t.status = 'in_progress' THEN 1 ELSE 0 END) as in_progress
        FROM tasks t
        JOIN projects p ON t.project_id = p.id
        GROUP BY p.id
        ORDER BY total_tasks DESC
    """
    rows = conn.execute(query).fetchall()
    results = []
    for r in rows:
        total = r["total_tasks"]
        results.append({
            "project_id": r["id"],
            "project_name": r["name"],
            "codename": r["codename"],
            "project_status": r["project_status"],
            "total_tasks": total,
            "done": r["done"],
            "blocked": r["blocked"],
            "todo": r["todo"],
            "in_progress": r["in_progress"],
            "completion_rate": round(r["done"] / total * 100, 1) if total else 0,
            "blocked_rate": round(r["blocked"] / total * 100, 1) if total else 0,
        })
    return results


def get_completion_rates_by_priority(conn):
    """Completion rates grouped by priority."""
    query = """
        SELECT priority,
               COUNT(*) as total,
               SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) as done,
               SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) as blocked
        FROM tasks
        GROUP BY priority
        ORDER BY
            CASE priority
                WHEN 'critical' THEN 1
                WHEN 'high' THEN 2
                WHEN 'medium' THEN 3
                WHEN 'low' THEN 4
                ELSE 5
            END
    """
    rows = conn.execute(query).fetchall()
    results = []
    for r in rows:
        total = r["total"]
        results.append({
            "priority": r["priority"],
            "total": total,
            "done": r["done"],
            "blocked": r["blocked"],
            "completion_rate": round(r["done"] / total * 100, 1) if total else 0,
            "blocked_rate": round(r["blocked"] / total * 100, 1) if total else 0,
        })
    return results


def get_completion_rates_by_complexity(conn):
    """Completion rates grouped by complexity."""
    query = """
        SELECT COALESCE(complexity, 'unset') as complexity,
               COUNT(*) as total,
               SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) as done,
               SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) as blocked
        FROM tasks
        GROUP BY complexity
        ORDER BY
            CASE complexity
                WHEN 'simple' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'complex' THEN 3
                WHEN 'epic' THEN 4
                ELSE 5
            END
    """
    rows = conn.execute(query).fetchall()
    results = []
    for r in rows:
        total = r["total"]
        results.append({
            "complexity": r["complexity"],
            "total": total,
            "done": r["done"],
            "blocked": r["blocked"],
            "completion_rate": round(r["done"] / total * 100, 1) if total else 0,
            "blocked_rate": round(r["blocked"] / total * 100, 1) if total else 0,
        })
    return results


def get_time_to_complete_by_complexity(conn):
    """Average time between created_at and completed_at, grouped by complexity.

    Only includes tasks where both timestamps are present.
    """
    query = """
        SELECT COALESCE(complexity, 'unset') as complexity,
               COUNT(*) as sample_size,
               AVG(
                   (julianday(completed_at) - julianday(created_at)) * 24 * 60
               ) as avg_minutes,
               MIN(
                   (julianday(completed_at) - julianday(created_at)) * 24 * 60
               ) as min_minutes,
               MAX(
                   (julianday(completed_at) - julianday(created_at)) * 24 * 60
               ) as max_minutes
        FROM tasks
        WHERE completed_at IS NOT NULL
          AND created_at IS NOT NULL
        GROUP BY complexity
        ORDER BY
            CASE complexity
                WHEN 'simple' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'complex' THEN 3
                WHEN 'epic' THEN 4
                ELSE 5
            END
    """
    rows = conn.execute(query).fetchall()
    results = []
    for r in rows:
        results.append({
            "complexity": r["complexity"],
            "sample_size": r["sample_size"],
            "avg_minutes": round(r["avg_minutes"], 1) if r["avg_minutes"] else None,
            "min_minutes": round(r["min_minutes"], 1) if r["min_minutes"] else None,
            "max_minutes": round(r["max_minutes"], 1) if r["max_minutes"] else None,
        })
    return results


def get_task_throughput_by_date(conn, days=30):
    """Tasks completed per day over the last N days."""
    query = """
        SELECT DATE(completed_at) as date, COUNT(*) as completed
        FROM tasks
        WHERE completed_at IS NOT NULL
          AND completed_at >= datetime('now', ?)
        GROUP BY DATE(completed_at)
        ORDER BY date
    """
    param = f"-{days} days"
    rows = conn.execute(query, (param,)).fetchall()
    return [{"date": r["date"], "completed": r["completed"]} for r in rows]


def get_blocked_tasks_detail(conn):
    """Get details about blocked tasks for error pattern analysis."""
    query = """
        SELECT t.id, t.project_id, p.name as project_name,
               t.title, t.priority, t.complexity, t.blocked_by
        FROM tasks t
        JOIN projects p ON t.project_id = p.id
        WHERE t.status = 'blocked'
        ORDER BY t.id
    """
    rows = conn.execute(query).fetchall()
    return [{
        "task_id": r["id"],
        "project_id": r["project_id"],
        "project_name": r["project_name"],
        "title": r["title"],
        "priority": r["priority"],
        "complexity": r["complexity"],
        "blocked_by": r["blocked_by"],
    } for r in rows]


def get_open_questions_summary(conn):
    """Summary of open questions (unresolved blockers)."""
    query = """
        SELECT oq.project_id, p.name as project_name,
               COUNT(*) as total,
               SUM(CASE WHEN oq.resolved = 0 THEN 1 ELSE 0 END) as unresolved,
               SUM(CASE WHEN oq.resolved = 1 THEN 1 ELSE 0 END) as resolved
        FROM open_questions oq
        JOIN projects p ON oq.project_id = p.id
        GROUP BY oq.project_id
        ORDER BY unresolved DESC
    """
    rows = conn.execute(query).fetchall()
    return [{
        "project_id": r["project_id"],
        "project_name": r["project_name"],
        "total": r["total"],
        "unresolved": r["unresolved"],
        "resolved": r["resolved"],
    } for r in rows]


def get_session_activity(conn, days=30):
    """Session note activity over recent period."""
    query = """
        SELECT COUNT(*) as total_sessions,
               COUNT(DISTINCT project_id) as projects_touched,
               COUNT(DISTINCT session_date) as active_days
        FROM session_notes
        WHERE session_date >= date('now', ?)
    """
    param = f"-{days} days"
    row = conn.execute(query, (param,)).fetchone()
    return {
        "total_sessions": row["total_sessions"],
        "projects_touched": row["projects_touched"],
        "active_days": row["active_days"],
        "period_days": days,
    }


def get_decisions_count_by_project(conn):
    """Count of architectural decisions per project."""
    query = """
        SELECT d.project_id, p.name as project_name, COUNT(*) as decisions
        FROM decisions d
        JOIN projects p ON d.project_id = p.id
        GROUP BY d.project_id
        ORDER BY decisions DESC
    """
    rows = conn.execute(query).fetchall()
    return [{
        "project_id": r["project_id"],
        "project_name": r["project_name"],
        "decisions": r["decisions"],
    } for r in rows]


# ---------------------------------------------------------------------------
# Checkpoint analysis
# ---------------------------------------------------------------------------

def analyze_checkpoints(checkpoints):
    """Aggregate checkpoint data into summary metrics."""
    if not checkpoints:
        return {
            "total_checkpoints": 0,
            "unique_tasks": 0,
            "total_attempts": 0,
            "continuation_frequency": {},
            "avg_turns_per_attempt": None,
            "avg_duration_seconds": None,
            "avg_cost_per_attempt": None,
            "total_cost_usd": 0,
            "outcome_distribution": {},
            "model_cost_breakdown": {},
            "error_patterns": [],
        }

    # Group by task
    tasks = defaultdict(list)
    for cp in checkpoints:
        tasks[cp["task_id"]].append(cp)

    # Continuation frequency: how many attempts per task
    attempt_counts = defaultdict(int)
    for task_id, cps in tasks.items():
        max_attempt = max(c["attempt"] for c in cps)
        attempt_counts[max_attempt] += 1

    # Outcome distribution
    outcomes = defaultdict(int)
    for cp in checkpoints:
        outcomes[cp["subtype"]] += 1

    # Model cost aggregation
    model_costs = defaultdict(lambda: {
        "total_cost_usd": 0, "total_input_tokens": 0,
        "total_output_tokens": 0, "total_cache_read_tokens": 0,
        "appearances": 0,
    })
    for cp in checkpoints:
        for model_id, usage in cp["model_usage"].items():
            mc = model_costs[model_id]
            mc["total_cost_usd"] += usage["cost_usd"]
            mc["total_input_tokens"] += usage["input_tokens"]
            mc["total_output_tokens"] += usage["output_tokens"]
            mc["total_cache_read_tokens"] += usage["cache_read_tokens"]
            mc["appearances"] += 1

    # Round model costs
    for model_id in model_costs:
        model_costs[model_id]["total_cost_usd"] = round(
            model_costs[model_id]["total_cost_usd"], 4
        )

    # Error patterns
    error_msgs = []
    for cp in checkpoints:
        for err in cp["errors"]:
            if isinstance(err, str) and err.strip():
                error_msgs.append(err.strip())

    # Deduplicate error patterns
    error_counts = defaultdict(int)
    for msg in error_msgs:
        error_counts[msg] += 1
    error_patterns = [
        {"message": msg, "count": cnt}
        for msg, cnt in sorted(error_counts.items(), key=lambda x: -x[1])
    ]

    total = len(checkpoints)
    durations = [cp["duration_ms"] / 1000 for cp in checkpoints if cp["duration_ms"]]
    turns = [cp["num_turns"] for cp in checkpoints if cp["num_turns"]]
    costs = [cp["total_cost_usd"] for cp in checkpoints if cp["total_cost_usd"]]

    return {
        "total_checkpoints": total,
        "unique_tasks": len(tasks),
        "total_attempts": total,
        "continuation_frequency": dict(
            sorted(
                {f"{k}_attempts": v for k, v in attempt_counts.items()}.items()
            )
        ),
        "avg_turns_per_attempt": round(sum(turns) / len(turns), 1) if turns else None,
        "avg_duration_seconds": round(sum(durations) / len(durations), 1) if durations else None,
        "avg_cost_per_attempt": round(sum(costs) / len(costs), 4) if costs else None,
        "total_cost_usd": round(sum(costs), 4) if costs else 0,
        "outcome_distribution": dict(outcomes),
        "model_cost_breakdown": dict(model_costs),
        "error_patterns": error_patterns,
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def build_report(conn, checkpoints_dir, project_id=None, days=30):
    """Build the full performance analysis report."""
    checkpoints = load_all_checkpoints(checkpoints_dir)
    checkpoint_analysis = analyze_checkpoints(checkpoints)

    # Global task stats
    task_stats = get_task_stats(conn, project_id)
    total_tasks = sum(task_stats.values())
    done = task_stats.get("done", 0)
    blocked = task_stats.get("blocked", 0)

    report = {
        "generated_at": datetime.now().isoformat(),
        "period_days": days,
        "filter_project_id": project_id,
        "summary": {
            "total_tasks": total_tasks,
            "done": done,
            "blocked": blocked,
            "todo": task_stats.get("todo", 0),
            "in_progress": task_stats.get("in_progress", 0),
            "overall_completion_rate": round(done / total_tasks * 100, 1) if total_tasks else 0,
            "overall_blocked_rate": round(blocked / total_tasks * 100, 1) if total_tasks else 0,
        },
        "completion_by_project": get_completion_rates_by_project(conn),
        "completion_by_priority": get_completion_rates_by_priority(conn),
        "completion_by_complexity": get_completion_rates_by_complexity(conn),
        "time_to_complete_by_complexity": get_time_to_complete_by_complexity(conn),
        "throughput_by_date": get_task_throughput_by_date(conn, days),
        "blocked_tasks": get_blocked_tasks_detail(conn),
        "open_questions": get_open_questions_summary(conn),
        "session_activity": get_session_activity(conn, days),
        "decisions_by_project": get_decisions_count_by_project(conn),
        "checkpoint_analysis": checkpoint_analysis,
    }

    return report


def print_summary(report):
    """Print a human-readable summary to stderr."""
    s = report["summary"]
    print(f"\n{'='*60}", file=sys.stderr)
    print("  EQUIPA Performance Report", file=sys.stderr)
    print(f"  Generated: {report['generated_at']}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    print(f"  Total tasks:      {s['total_tasks']}", file=sys.stderr)
    print(f"  Done:             {s['done']} ({s['overall_completion_rate']}%)", file=sys.stderr)
    print(f"  Blocked:          {s['blocked']} ({s['overall_blocked_rate']}%)", file=sys.stderr)
    print(f"  Todo:             {s['todo']}", file=sys.stderr)
    print(f"  In Progress:      {s['in_progress']}", file=sys.stderr)

    # Complexity time-to-complete
    ttc = report.get("time_to_complete_by_complexity", [])
    if ttc:
        print(f"\n  Time-to-Complete by Complexity:", file=sys.stderr)
        for entry in ttc:
            avg = entry["avg_minutes"]
            if avg is not None:
                label = entry["complexity"]
                print(f"    {label:>10}: {avg:>8.1f} min avg  (n={entry['sample_size']})", file=sys.stderr)

    # Checkpoint summary
    ca = report.get("checkpoint_analysis", {})
    if ca.get("total_checkpoints", 0) > 0:
        print(f"\n  Checkpoint Analysis:", file=sys.stderr)
        print(f"    Total checkpoints:    {ca['total_checkpoints']}", file=sys.stderr)
        print(f"    Unique tasks:         {ca['unique_tasks']}", file=sys.stderr)
        print(f"    Avg turns/attempt:    {ca['avg_turns_per_attempt']}", file=sys.stderr)
        print(f"    Avg duration:         {ca['avg_duration_seconds']}s", file=sys.stderr)
        print(f"    Avg cost/attempt:     ${ca['avg_cost_per_attempt']}", file=sys.stderr)
        print(f"    Total cost:           ${ca['total_cost_usd']}", file=sys.stderr)
        if ca.get("outcome_distribution"):
            print(f"    Outcomes:             {ca['outcome_distribution']}", file=sys.stderr)

    # Throughput
    throughput = report.get("throughput_by_date", [])
    if throughput:
        total_completed = sum(d["completed"] for d in throughput)
        active_days = len(throughput)
        avg_per_day = round(total_completed / active_days, 1) if active_days else 0
        print(f"\n  Throughput (last {report['period_days']} days):", file=sys.stderr)
        print(f"    Tasks completed:      {total_completed}", file=sys.stderr)
        print(f"    Active days:          {active_days}", file=sys.stderr)
        print(f"    Avg tasks/day:        {avg_per_day}", file=sys.stderr)

    print(f"\n{'='*60}\n", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="EQUIPA Performance Analysis — parse orchestrator data and produce a JSON report"
    )
    parser.add_argument(
        "--project", type=int, default=None,
        help="Filter to a specific project ID"
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Look-back period in days (default: 30)"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Write JSON report to file (default: stdout)"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress human-readable summary on stderr"
    )
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    config = load_config(script_dir)
    conn = connect_db(config)

    checkpoints_dir = os.path.join(script_dir, ".forge-checkpoints")

    try:
        report = build_report(conn, checkpoints_dir, args.project, args.days)
    finally:
        conn.close()

    if not args.quiet:
        print_summary(report)

    json_output = json.dumps(report, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(json_output)
            f.write("\n")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(json_output)


if __name__ == "__main__":
    main()

