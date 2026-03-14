#!/usr/bin/env python3
"""
EQUIPA Agent Performance Dashboard
Copyright 2026, Forgeborn

Generates a self-contained HTML report showing EQUIPA metrics:
  - Tasks completed/failed per day
  - Average turns per agent run
  - Model usage and cost estimates
  - Agent success rates by role
  - Project completion rates
  - Blocked tasks and open questions

Reads from TheForge SQLite DB and orchestrator checkpoint files.

Usage:
    python forge_dashboard.py                     # Generate report to stdout
    python forge_dashboard.py --output report.html  # Write to file
    python forge_dashboard.py --days 7             # Last 7 days only
    python forge_dashboard.py --open               # Generate and open in browser
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime
from html import escape
from pathlib import Path


def load_config(script_dir):
    """Load forge_config.json for DB path."""
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
# Checkpoint parsing (reused from analyze_performance.py)
# ---------------------------------------------------------------------------

def parse_checkpoint_filename(filename):
    """Extract task_id, role, attempt from checkpoint filename."""
    match = re.match(r"task_(\d+)_(\w+)_attempt_(\d+)\.txt", filename)
    if not match:
        return None
    return {
        "task_id": int(match.group(1)),
        "role": match.group(2),
        "attempt": int(match.group(3)),
    }


def parse_checkpoint_file(filepath):
    """Parse a single checkpoint JSON file."""
    try:
        with open(filepath, "r") as f:
            data = json.loads(f.read().strip())
    except (json.JSONDecodeError, OSError):
        return None

    result = {
        "subtype": data.get("subtype", "unknown"),
        "duration_ms": data.get("duration_ms", 0),
        "num_turns": data.get("num_turns", 0),
        "total_cost_usd": data.get("total_cost_usd", 0.0),
        "is_error": data.get("is_error", False),
        "model_usage": {},
    }

    for model_id, usage in data.get("modelUsage", {}).items():
        result["model_usage"][model_id] = {
            "input_tokens": usage.get("inputTokens", 0),
            "output_tokens": usage.get("outputTokens", 0),
            "cache_read_tokens": usage.get("cacheReadInputTokens", 0),
            "cost_usd": usage.get("costUSD", 0.0),
        }

    return result


def load_all_checkpoints(checkpoints_dir):
    """Load all checkpoint files."""
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

def query_task_summary(conn):
    """Overall task status counts."""
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
    ).fetchall()
    return {r["status"]: r["cnt"] for r in rows}


def query_tasks_per_day(conn, days=30):
    """Tasks completed per day."""
    rows = conn.execute("""
        SELECT DATE(completed_at) as day, COUNT(*) as completed
        FROM tasks
        WHERE completed_at IS NOT NULL
          AND completed_at >= datetime('now', ?)
        GROUP BY DATE(completed_at)
        ORDER BY day
    """, (f"-{days} days",)).fetchall()
    return [{"day": r["day"], "completed": r["completed"]} for r in rows]


def query_project_completion(conn):
    """Per-project task completion rates."""
    rows = conn.execute("""
        SELECT p.name, p.codename, p.status as proj_status,
               COUNT(*) as total,
               SUM(CASE WHEN t.status='done' THEN 1 ELSE 0 END) as done,
               SUM(CASE WHEN t.status='blocked' THEN 1 ELSE 0 END) as blocked,
               SUM(CASE WHEN t.status='todo' THEN 1 ELSE 0 END) as todo,
               SUM(CASE WHEN t.status='in_progress' THEN 1 ELSE 0 END) as in_progress
        FROM tasks t JOIN projects p ON t.project_id = p.id
        GROUP BY p.id ORDER BY total DESC
    """).fetchall()
    results = []
    for r in rows:
        total = r["total"]
        results.append({
            "name": r["name"],
            "codename": r["codename"],
            "status": r["proj_status"],
            "total": total,
            "done": r["done"],
            "blocked": r["blocked"],
            "todo": r["todo"],
            "in_progress": r["in_progress"],
            "completion_pct": round(r["done"] / total * 100, 1) if total else 0,
        })
    return results


def query_priority_breakdown(conn):
    """Task counts by priority (named priorities only)."""
    rows = conn.execute("""
        SELECT priority,
               COUNT(*) as total,
               SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done,
               SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) as blocked
        FROM tasks
        WHERE priority IN ('critical', 'high', 'medium', 'low')
        GROUP BY priority
        ORDER BY CASE priority
            WHEN 'critical' THEN 1 WHEN 'high' THEN 2
            WHEN 'medium' THEN 3 WHEN 'low' THEN 4 END
    """).fetchall()
    return [dict(r) for r in rows]


def query_blocked_tasks(conn):
    """Details of currently blocked tasks."""
    rows = conn.execute("""
        SELECT t.id, p.codename, t.title, t.priority, t.blocked_by
        FROM tasks t JOIN projects p ON t.project_id = p.id
        WHERE t.status = 'blocked'
        ORDER BY t.id
    """).fetchall()
    return [dict(r) for r in rows]


def query_open_questions(conn):
    """Unresolved open questions."""
    rows = conn.execute("""
        SELECT oq.id, p.codename, oq.question, oq.context
        FROM open_questions oq
        JOIN projects p ON oq.project_id = p.id
        WHERE oq.resolved = 0
        ORDER BY oq.id DESC
        LIMIT 20
    """).fetchall()
    return [dict(r) for r in rows]


def query_session_activity(conn, days=30):
    """Session notes activity."""
    row = conn.execute("""
        SELECT COUNT(*) as total_sessions,
               COUNT(DISTINCT project_id) as projects_touched,
               COUNT(DISTINCT session_date) as active_days
        FROM session_notes
        WHERE session_date >= date('now', ?)
    """, (f"-{days} days",)).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# Checkpoint analysis
# ---------------------------------------------------------------------------

def analyze_checkpoints(checkpoints):
    """Aggregate checkpoint data for dashboard metrics."""
    if not checkpoints:
        return {
            "total_runs": 0,
            "unique_tasks": 0,
            "by_role": {},
            "by_model": {},
            "by_outcome": {},
            "avg_turns": 0,
            "avg_duration_min": 0,
            "avg_cost": 0,
            "total_cost": 0,
        }

    tasks = defaultdict(list)
    for cp in checkpoints:
        tasks[cp["task_id"]].append(cp)

    # By role
    role_stats = defaultdict(lambda: {"runs": 0, "errors": 0, "total_turns": 0, "total_cost": 0})
    for cp in checkpoints:
        r = role_stats[cp["role"]]
        r["runs"] += 1
        if cp["is_error"] or cp["subtype"].startswith("error"):
            r["errors"] += 1
        r["total_turns"] += cp["num_turns"]
        r["total_cost"] += cp["total_cost_usd"]

    for role in role_stats:
        rs = role_stats[role]
        rs["success_rate"] = round((rs["runs"] - rs["errors"]) / rs["runs"] * 100, 1) if rs["runs"] else 0
        rs["avg_turns"] = round(rs["total_turns"] / rs["runs"], 1) if rs["runs"] else 0
        rs["total_cost"] = round(rs["total_cost"], 4)

    # By model
    model_stats = defaultdict(lambda: {"cost": 0, "input_tokens": 0, "output_tokens": 0, "appearances": 0})
    for cp in checkpoints:
        for model_id, usage in cp["model_usage"].items():
            ms = model_stats[model_id]
            ms["cost"] += usage["cost_usd"]
            ms["input_tokens"] += usage["input_tokens"]
            ms["output_tokens"] += usage["output_tokens"]
            ms["appearances"] += 1
    for m in model_stats:
        model_stats[m]["cost"] = round(model_stats[m]["cost"], 4)

    # By outcome
    outcomes = defaultdict(int)
    for cp in checkpoints:
        outcomes[cp["subtype"]] += 1

    turns = [cp["num_turns"] for cp in checkpoints if cp["num_turns"]]
    durations = [cp["duration_ms"] / 60000 for cp in checkpoints if cp["duration_ms"]]
    costs = [cp["total_cost_usd"] for cp in checkpoints if cp["total_cost_usd"]]

    return {
        "total_runs": len(checkpoints),
        "unique_tasks": len(tasks),
        "by_role": dict(role_stats),
        "by_model": dict(model_stats),
        "by_outcome": dict(outcomes),
        "avg_turns": round(sum(turns) / len(turns), 1) if turns else 0,
        "avg_duration_min": round(sum(durations) / len(durations), 1) if durations else 0,
        "avg_cost": round(sum(costs) / len(costs), 4) if costs else 0,
        "total_cost": round(sum(costs), 4) if costs else 0,
    }


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def _bar_chart_html(data, label_key, value_key, max_val=None, color="#58a6ff", suffix=""):
    """Generate a simple CSS bar chart."""
    if not data:
        return '<p class="muted">No data available</p>'
    if max_val is None:
        max_val = max(d[value_key] for d in data) or 1
    rows = []
    for d in data:
        val = d[value_key]
        pct = (val / max_val * 100) if max_val else 0
        label = escape(str(d[label_key]))
        rows.append(f'''
            <div class="bar-row">
                <div class="bar-label">{label}</div>
                <div class="bar-track">
                    <div class="bar-fill" style="width: {pct:.1f}%; background: {color};"></div>
                </div>
                <div class="bar-value">{val}{suffix}</div>
            </div>''')
    return "\n".join(rows)


def _stacked_bar_html(projects):
    """Generate stacked horizontal bars for project completion."""
    if not projects:
        return '<p class="muted">No data available</p>'
    rows = []
    for p in projects:
        total = p["total"]
        if total == 0:
            continue
        done_pct = p["done"] / total * 100
        blocked_pct = p["blocked"] / total * 100
        in_progress_pct = p["in_progress"] / total * 100
        todo_pct = p["todo"] / total * 100
        name = escape(p["codename"])
        rows.append(f'''
            <div class="bar-row">
                <div class="bar-label" title="{escape(p['name'])}">{name}</div>
                <div class="bar-track stacked">
                    <div class="bar-fill done" style="width: {done_pct:.1f}%;" title="Done: {p['done']}"></div>
                    <div class="bar-fill in-progress" style="width: {in_progress_pct:.1f}%;" title="In Progress: {p['in_progress']}"></div>
                    <div class="bar-fill blocked" style="width: {blocked_pct:.1f}%;" title="Blocked: {p['blocked']}"></div>
                    <div class="bar-fill todo-bar" style="width: {todo_pct:.1f}%;" title="Todo: {p['todo']}"></div>
                </div>
                <div class="bar-value">{p['completion_pct']:.0f}%</div>
            </div>''')
    return "\n".join(rows)


def _table_html(headers, rows):
    """Generate an HTML table."""
    ths = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = ""
    for row in rows:
        tds = "".join(f"<td>{escape(str(v))}</td>" for v in row)
        body += f"<tr>{tds}</tr>\n"
    return f"""
        <table>
            <thead><tr>{ths}</tr></thead>
            <tbody>{body}</tbody>
        </table>"""


def generate_html(task_summary, tasks_per_day, project_completion, priority_breakdown,
                  blocked_tasks, open_questions, session_activity, checkpoint_data, days):
    """Generate the complete HTML dashboard."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_tasks = sum(task_summary.values())
    done = task_summary.get("done", 0)
    blocked = task_summary.get("blocked", 0)
    todo = task_summary.get("todo", 0)
    in_progress = task_summary.get("in_progress", 0)
    completion_rate = round(done / total_tasks * 100, 1) if total_tasks else 0

    # Throughput summary
    total_completed_period = sum(d["completed"] for d in tasks_per_day)
    active_days = len(tasks_per_day)
    avg_per_day = round(total_completed_period / active_days, 1) if active_days else 0

    # Tasks per day chart
    tasks_per_day_chart = _bar_chart_html(tasks_per_day, "day", "completed", color="#3fb950")

    # Project completion chart
    project_chart = _stacked_bar_html(project_completion)

    # Priority breakdown table
    priority_rows = []
    for p in priority_breakdown:
        total = p["total"]
        rate = round(p["done"] / total * 100, 1) if total else 0
        priority_rows.append([p["priority"].title(), str(total), str(p["done"]),
                              str(p["blocked"]), f"{rate}%"])

    priority_table = _table_html(
        ["Priority", "Total", "Done", "Blocked", "Rate"],
        priority_rows
    )

    # Blocked tasks table
    blocked_rows = []
    for bt in blocked_tasks:
        blocked_rows.append([
            str(bt["id"]), bt["codename"], bt["title"][:60],
            bt["priority"] or "?", bt["blocked_by"] or "unknown"
        ])
    blocked_table = _table_html(
        ["ID", "Project", "Title", "Priority", "Blocked By"],
        blocked_rows
    ) if blocked_rows else '<p class="muted">No blocked tasks</p>'

    # Open questions table
    oq_rows = []
    for oq in open_questions:
        oq_rows.append([str(oq["id"]), oq["codename"], oq["question"][:80]])
    oq_table = _table_html(
        ["ID", "Project", "Question"],
        oq_rows
    ) if oq_rows else '<p class="muted">No unresolved questions</p>'

    # Checkpoint / agent metrics
    cp = checkpoint_data
    agent_runs_html = ""
    if cp["total_runs"] > 0:
        # Role stats table
        role_rows = []
        for role, stats in sorted(cp["by_role"].items()):
            role_rows.append([
                role.title(), str(stats["runs"]), f"{stats['success_rate']}%",
                str(stats["avg_turns"]), f"${stats['total_cost']:.2f}"
            ])
        role_table = _table_html(
            ["Role", "Runs", "Success Rate", "Avg Turns", "Total Cost"],
            role_rows
        )

        # Model stats table
        model_rows = []
        for model_id, stats in sorted(cp["by_model"].items(), key=lambda x: -x[1]["cost"]):
            short_name = model_id.replace("claude-", "").split("-2025")[0]
            model_rows.append([
                short_name, str(stats["appearances"]),
                f"{stats['input_tokens']:,}", f"{stats['output_tokens']:,}",
                f"${stats['cost']:.2f}"
            ])
        model_table = _table_html(
            ["Model", "Uses", "Input Tokens", "Output Tokens", "Cost"],
            model_rows
        )

        # Outcome chart
        outcome_data = [{"outcome": k, "count": v} for k, v in
                        sorted(cp["by_outcome"].items(), key=lambda x: -x[1])]
        outcome_chart = _bar_chart_html(outcome_data, "outcome", "count", color="#8b5cf6")

        agent_runs_html = f"""
        <div class="section">
            <h2>Agent Performance (from Checkpoints)</h2>
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-value">{cp['total_runs']}</div>
                    <div class="stat-label">Total Runs</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{cp['unique_tasks']}</div>
                    <div class="stat-label">Unique Tasks</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{cp['avg_turns']}</div>
                    <div class="stat-label">Avg Turns/Run</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{cp['avg_duration_min']:.1f}m</div>
                    <div class="stat-label">Avg Duration</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${cp['avg_cost']:.2f}</div>
                    <div class="stat-label">Avg Cost/Run</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${cp['total_cost']:.2f}</div>
                    <div class="stat-label">Total Cost</div>
                </div>
            </div>

            <h3>Success Rate by Role</h3>
            {role_table}

            <h3>Model Usage & Cost</h3>
            {model_table}

            <h3>Run Outcomes</h3>
            <div class="chart-container">
                {outcome_chart}
            </div>
        </div>"""
    else:
        agent_runs_html = """
        <div class="section">
            <h2>Agent Performance (from Checkpoints)</h2>
            <p class="muted">No checkpoint files found in .forge-checkpoints/. Agent run data will appear here after orchestrator runs.</p>
        </div>"""

    # Session activity
    sa = session_activity
    session_html = f"""
        <div class="stats-grid" style="grid-template-columns: repeat(3, 1fr);">
            <div class="stat-card">
                <div class="stat-value">{sa.get('total_sessions', 0)}</div>
                <div class="stat-label">Sessions ({days}d)</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{sa.get('projects_touched', 0)}</div>
                <div class="stat-label">Projects Active</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{sa.get('active_days', 0)}</div>
                <div class="stat-label">Active Days</div>
            </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EQUIPA Agent Performance Dashboard</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    background: #0a0e17;
    color: #c9d1d9;
    line-height: 1.6;
    padding: 40px 20px;
  }}
  .container {{ max-width: 1000px; margin: 0 auto; }}

  /* Header */
  .header {{
    text-align: center;
    margin-bottom: 40px;
    padding-bottom: 28px;
    border-bottom: 1px solid #21262d;
  }}
  .header h1 {{
    font-size: 2.2em;
    color: #e6edf3;
    font-weight: 700;
    letter-spacing: -0.5px;
  }}
  .header h1 span {{ color: #58a6ff; }}
  .header .subtitle {{
    font-size: 1em;
    color: #8b949e;
    margin-top: 6px;
  }}
  .header .badge {{
    display: inline-block;
    background: #1a3a2a;
    color: #3fb950;
    padding: 3px 12px;
    border-radius: 20px;
    font-size: 0.82em;
    font-weight: 600;
    margin-top: 12px;
    border: 1px solid #2a5a3a;
  }}

  /* Sections */
  .section {{
    background: #0d1117;
    border: 1px solid #21262d;
    border-radius: 8px;
    padding: 24px 28px;
    margin-bottom: 20px;
  }}
  .section h2 {{
    font-size: 1.25em;
    color: #e6edf3;
    margin-bottom: 14px;
    padding-bottom: 8px;
    border-bottom: 1px solid #21262d;
  }}
  .section h3 {{
    font-size: 1em;
    color: #58a6ff;
    margin: 18px 0 10px 0;
  }}

  /* Stats grid */
  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    margin: 16px 0;
  }}
  .stat-card {{
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 6px;
    padding: 16px;
    text-align: center;
  }}
  .stat-value {{
    font-size: 1.8em;
    font-weight: 700;
    color: #e6edf3;
    line-height: 1.2;
  }}
  .stat-value.green {{ color: #3fb950; }}
  .stat-value.red {{ color: #f85149; }}
  .stat-value.blue {{ color: #58a6ff; }}
  .stat-value.yellow {{ color: #d29922; }}
  .stat-label {{
    font-size: 0.82em;
    color: #8b949e;
    margin-top: 4px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}

  /* Bar chart */
  .chart-container {{ margin: 12px 0; }}
  .bar-row {{
    display: flex;
    align-items: center;
    margin-bottom: 6px;
  }}
  .bar-label {{
    width: 120px;
    font-size: 0.82em;
    color: #8b949e;
    text-align: right;
    padding-right: 10px;
    flex-shrink: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .bar-track {{
    flex: 1;
    height: 22px;
    background: #161b22;
    border-radius: 4px;
    overflow: hidden;
    position: relative;
  }}
  .bar-track.stacked {{
    display: flex;
  }}
  .bar-fill {{
    height: 100%;
    border-radius: 4px;
    transition: width 0.3s;
  }}
  .bar-fill.done {{ background: #3fb950; border-radius: 4px 0 0 4px; }}
  .bar-fill.in-progress {{ background: #58a6ff; border-radius: 0; }}
  .bar-fill.blocked {{ background: #f85149; border-radius: 0; }}
  .bar-fill.todo-bar {{ background: #484f58; border-radius: 0 4px 4px 0; }}
  .bar-value {{
    width: 60px;
    font-size: 0.82em;
    color: #e6edf3;
    text-align: right;
    padding-left: 8px;
    flex-shrink: 0;
  }}

  /* Legend */
  .legend {{
    display: flex;
    gap: 16px;
    margin: 10px 0 6px 0;
    font-size: 0.8em;
    color: #8b949e;
  }}
  .legend-item {{
    display: flex;
    align-items: center;
    gap: 4px;
  }}
  .legend-dot {{
    width: 10px;
    height: 10px;
    border-radius: 2px;
    display: inline-block;
  }}

  /* Tables */
  table {{
    width: 100%;
    border-collapse: collapse;
    margin: 10px 0;
    font-size: 0.88em;
  }}
  th {{
    text-align: left;
    padding: 8px 10px;
    color: #8b949e;
    border-bottom: 1px solid #21262d;
    font-weight: 600;
    text-transform: uppercase;
    font-size: 0.85em;
    letter-spacing: 0.3px;
  }}
  td {{
    padding: 7px 10px;
    border-bottom: 1px solid #161b22;
    color: #c9d1d9;
  }}
  tr:hover td {{ background: #161b22; }}

  .muted {{ color: #484f58; font-style: italic; }}

  /* Footer */
  .footer {{
    text-align: center;
    margin-top: 32px;
    padding-top: 20px;
    border-top: 1px solid #21262d;
    font-size: 0.82em;
    color: #484f58;
  }}

  /* Responsive */
  @media (max-width: 700px) {{
    .stats-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .bar-label {{ width: 80px; font-size: 0.75em; }}
  }}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <h1>Forge<span>Team</span> Dashboard</h1>
    <div class="subtitle">Agent Performance Report</div>
    <div class="badge">Generated {now} | Last {days} days</div>
  </div>

  <!-- Summary Cards -->
  <div class="section">
    <h2>Task Overview</h2>
    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-value">{total_tasks}</div>
        <div class="stat-label">Total Tasks</div>
      </div>
      <div class="stat-card">
        <div class="stat-value green">{done}</div>
        <div class="stat-label">Done</div>
      </div>
      <div class="stat-card">
        <div class="stat-value blue">{in_progress}</div>
        <div class="stat-label">In Progress</div>
      </div>
      <div class="stat-card">
        <div class="stat-value yellow">{todo}</div>
        <div class="stat-label">Todo</div>
      </div>
    </div>
    <div class="stats-grid" style="grid-template-columns: repeat(3, 1fr);">
      <div class="stat-card">
        <div class="stat-value green">{completion_rate}%</div>
        <div class="stat-label">Completion Rate</div>
      </div>
      <div class="stat-card">
        <div class="stat-value red">{blocked}</div>
        <div class="stat-label">Blocked</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{avg_per_day}</div>
        <div class="stat-label">Avg Tasks/Day</div>
      </div>
    </div>
  </div>

  <!-- Tasks Per Day -->
  <div class="section">
    <h2>Tasks Completed Per Day (Last {days} Days)</h2>
    <div class="chart-container">
      {tasks_per_day_chart}
    </div>
    <p class="muted" style="margin-top: 8px;">
      {total_completed_period} tasks completed across {active_days} active days
    </p>
  </div>

  <!-- Project Completion -->
  <div class="section">
    <h2>Project Completion</h2>
    <div class="legend">
      <div class="legend-item"><span class="legend-dot" style="background:#3fb950;"></span> Done</div>
      <div class="legend-item"><span class="legend-dot" style="background:#58a6ff;"></span> In Progress</div>
      <div class="legend-item"><span class="legend-dot" style="background:#f85149;"></span> Blocked</div>
      <div class="legend-item"><span class="legend-dot" style="background:#484f58;"></span> Todo</div>
    </div>
    <div class="chart-container">
      {project_chart}
    </div>
  </div>

  <!-- Priority Breakdown -->
  <div class="section">
    <h2>Task Priority Breakdown</h2>
    {priority_table}
  </div>

  <!-- Agent Performance -->
  {agent_runs_html}

  <!-- Session Activity -->
  <div class="section">
    <h2>Session Activity (Last {days} Days)</h2>
    {session_html}
  </div>

  <!-- Blocked Tasks -->
  <div class="section">
    <h2>Blocked Tasks</h2>
    {blocked_table}
  </div>

  <!-- Open Questions -->
  <div class="section">
    <h2>Unresolved Questions</h2>
    {oq_table}
  </div>

  <div class="footer">
    Copyright 2026, Forgeborn | EQUIPA Agent Performance Dashboard
  </div>

</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="EQUIPA Agent Performance Dashboard — HTML report generator"
    )
    parser.add_argument("--days", type=int, default=30, help="Look-back period in days (default: 30)")
    parser.add_argument("--output", type=str, default=None, help="Write HTML to file (default: stdout)")
    parser.add_argument("--open", action="store_true", help="Generate to file and open in browser")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    config = load_config(script_dir)
    conn = connect_db(config)

    checkpoints_dir = os.path.join(script_dir, ".forge-checkpoints")

    try:
        task_summary = query_task_summary(conn)
        tasks_per_day = query_tasks_per_day(conn, args.days)
        project_completion = query_project_completion(conn)
        priority_breakdown = query_priority_breakdown(conn)
        blocked_tasks = query_blocked_tasks(conn)
        open_questions = query_open_questions(conn)
        session_activity = query_session_activity(conn, args.days)
    finally:
        conn.close()

    checkpoint_data = analyze_checkpoints(load_all_checkpoints(checkpoints_dir))

    html = generate_html(
        task_summary, tasks_per_day, project_completion, priority_breakdown,
        blocked_tasks, open_questions, session_activity, checkpoint_data, args.days
    )

    output_path = args.output
    if args.open and not output_path:
        output_path = os.path.join(script_dir, "forge-dashboard.html")

    if output_path:
        with open(output_path, "w") as f:
            f.write(html)
        print(f"Dashboard written to {output_path}", file=sys.stderr)
        if args.open:
            webbrowser.open(f"file://{os.path.abspath(output_path)}")
    else:
        print(html)


if __name__ == "__main__":
    main()
