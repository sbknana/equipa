#!/usr/bin/env python3
"""EQUIPA — Nightly Portfolio Review

Runs nightly to review all projects, flag stale work, and track progress.
Outputs a markdown report and optionally prints to stdout.

What it does:
1. Reviews all active projects in TheForge database
2. Lists what was accomplished in the last 24 hours
3. Identifies blocked and stale tasks
4. Flags stale projects (no activity in 7+ days)
5. Summarizes agent performance (success rates, costs)
6. Saves a dated review file

Setup (cron):
    # Run nightly at 11 PM (adjust timezone as needed)
    0 23 * * * cd /path/to/equipa && python3 nightly_review.py

Usage:
    python3 nightly_review.py              # Generate review, save to file
    python3 nightly_review.py --print      # Also print full review to stdout
    python3 nightly_review.py --db /path/to/theforge.db  # Custom DB path

Stdlib only — no pip dependencies required.

Copyright 2026 Forgeborn
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# --- Auto-detect database ---

def find_db(explicit_path=None):
    """Find theforge.db — explicit path, env var, or scan common locations."""
    if explicit_path:
        return explicit_path

    if os.environ.get("THEFORGE_DB"):
        return os.environ["THEFORGE_DB"]

    # Check current directory and parent
    for candidate in [
        Path.cwd() / "theforge.db",
        Path.cwd().parent / "theforge.db",
        Path(__file__).resolve().parent / "theforge.db",
    ]:
        if candidate.exists():
            return str(candidate)

    print("ERROR: Cannot find theforge.db")
    print("  Set THEFORGE_DB env var or pass --db /path/to/theforge.db")
    sys.exit(1)


def get_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# --- Data queries ---

def get_portfolio_stats(conn):
    """Get overall portfolio health."""
    cur = conn.cursor()
    stats = {}
    for label, query in [
        ("active_projects", "SELECT COUNT(*) FROM projects WHERE status = 'active'"),
        ("complete_projects", "SELECT COUNT(*) FROM projects WHERE status = 'complete'"),
        ("done_tasks", "SELECT COUNT(*) FROM tasks WHERE status = 'done'"),
        ("todo_tasks", "SELECT COUNT(*) FROM tasks WHERE status = 'todo'"),
        ("blocked_tasks", "SELECT COUNT(*) FROM tasks WHERE status = 'blocked'"),
        ("in_progress", "SELECT COUNT(*) FROM tasks WHERE status = 'in_progress'"),
    ]:
        cur.execute(query)
        stats[label] = cur.fetchone()[0]
    return stats


def get_today_accomplishments(conn):
    """Tasks completed in the last 24 hours."""
    cur = conn.cursor()
    cur.execute("""
        SELECT t.id, p.codename, t.title
        FROM tasks t
        JOIN projects p ON t.project_id = p.id
        WHERE t.status = 'done'
        AND t.completed_at >= datetime('now', '-24 hours')
        ORDER BY t.completed_at DESC
    """)
    return [dict(r) for r in cur.fetchall()]


def get_blockers(conn):
    """Get all blocked tasks."""
    cur = conn.cursor()
    cur.execute("""
        SELECT t.id, p.codename, t.title, t.blocked_by
        FROM tasks t
        JOIN projects p ON t.project_id = p.id
        WHERE t.status = 'blocked'
        ORDER BY t.id
    """)
    return [dict(r) for r in cur.fetchall()]


def get_stale_projects(conn):
    """Projects with no task activity in 7+ days."""
    cur = conn.cursor()
    cur.execute("""
        SELECT p.codename, p.name, p.status,
            MAX(t.completed_at) as last_activity
        FROM projects p
        LEFT JOIN tasks t ON t.project_id = p.id AND t.status = 'done'
        WHERE p.status = 'active'
        GROUP BY p.id
        HAVING last_activity < datetime('now', '-7 days')
        OR last_activity IS NULL
        ORDER BY last_activity ASC
    """)
    return [dict(r) for r in cur.fetchall()]


def get_stale_tasks(conn):
    """Tasks stuck in_progress for 3+ days."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM v_stale_tasks LIMIT 20")
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        # View might not exist — query directly
        cur.execute("""
            SELECT t.id, p.codename as project_name, t.title,
                ROUND(julianday('now') - julianday(t.created_at)) as days_stale
            FROM tasks t
            JOIN projects p ON t.project_id = p.id
            WHERE t.status = 'in_progress'
            AND julianday('now') - julianday(t.created_at) > 3
            ORDER BY days_stale DESC
        """)
        return [dict(r) for r in cur.fetchall()]


def get_agent_stats(conn):
    """Agent performance in the last 24 hours."""
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                COUNT(*) as total_runs,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes,
                ROUND(SUM(cost_usd), 2) as total_cost,
                ROUND(AVG(num_turns), 1) as avg_turns
            FROM agent_runs
            WHERE started_at >= datetime('now', '-24 hours')
        """)
        row = cur.fetchone()
        if row and row["total_runs"] > 0:
            return dict(row)
    except sqlite3.OperationalError:
        pass
    return None


def get_open_questions(conn):
    """Unresolved questions older than 7 days."""
    cur = conn.cursor()
    cur.execute("""
        SELECT oq.id, p.codename, oq.question,
            ROUND(julianday('now') - julianday(oq.asked_at)) as days_open
        FROM open_questions oq
        JOIN projects p ON oq.project_id = p.id
        WHERE oq.resolved = 0
        AND julianday('now') - julianday(oq.asked_at) > 7
        ORDER BY days_open DESC
        LIMIT 10
    """)
    return [dict(r) for r in cur.fetchall()]


def get_upcoming_reminders(conn):
    """Reminders due in the next 7 days."""
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT r.title, p.codename as project_name,
                ROUND(julianday(r.reminder_date) - julianday('now')) as days_until
            FROM reminders r
            LEFT JOIN projects p ON r.project_id = p.id
            WHERE r.status = 'pending'
            AND julianday(r.reminder_date) - julianday('now') <= 7
            ORDER BY r.reminder_date ASC
            LIMIT 10
        """)
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        return []


# --- Report generation ---

def generate_review(stats, accomplishments, blockers, stale_projects,
                    stale_tasks, agent_stats, open_questions, reminders):
    """Generate the nightly review document."""
    today = datetime.now().strftime("%Y-%m-%d")

    lines = [
        f"# Nightly Portfolio Review — {today}",
        "",
        "## Portfolio Health",
        f"- **{stats['active_projects']}** active projects, **{stats['complete_projects']}** completed",
        f"- Tasks: **{stats['done_tasks']}** done / **{stats['todo_tasks']}** todo / **{stats['in_progress']}** in progress / **{stats['blocked_tasks']}** blocked",
        "",
    ]

    # Agent performance
    if agent_stats and agent_stats["total_runs"] > 0:
        rate = round(agent_stats["successes"] / agent_stats["total_runs"] * 100, 1) if agent_stats["total_runs"] > 0 else 0
        lines.extend([
            "## Agent Performance (Last 24h)",
            f"- **{agent_stats['total_runs']}** agent runs, **{rate}%** success rate",
            f"- Cost: **${agent_stats['total_cost']}**, avg turns: **{agent_stats['avg_turns']}**",
            "",
        ])

    # Today's accomplishments
    lines.append("## Today's Accomplishments")
    if accomplishments:
        for a in accomplishments:
            lines.append(f"- [{a['codename']}] #{a['id']}: {a['title']}")
    else:
        lines.append("- No tasks completed today")

    # Reminders
    if reminders:
        lines.extend(["", "## Reminders"])
        for r in reminders:
            status = "OVERDUE" if r["days_until"] < 0 else f"in {int(r['days_until'])}d"
            project = f"[{r['project_name']}] " if r.get("project_name") else ""
            lines.append(f"- {project}{r['title']} — {status}")

    # Blockers
    lines.extend(["", "## Blocked Tasks"])
    if blockers:
        for b in blockers:
            lines.append(f"- #{b['id']} [{b['codename']}]: {b['title']}")
    else:
        lines.append("- No blocked tasks!")

    # Stale tasks
    if stale_tasks:
        lines.extend(["", "## Stale Tasks (in_progress 3+ days)"])
        for t in stale_tasks:
            name = t.get("project_name", t.get("codename", "?"))
            lines.append(f"- [{name}] {t.get('title', '?')} — {int(t.get('days_stale', 0))} days")

    # Open questions
    if open_questions:
        lines.extend(["", "## Old Open Questions (7+ days)"])
        for q in open_questions:
            lines.append(f"- [{q['codename']}] {q['question']} — {int(q['days_open'])}d open")

    # Stale projects
    lines.extend(["", "## Stale Projects (7+ days inactive)"])
    if stale_projects:
        for s in stale_projects:
            lines.append(f"- **{s['codename']}** ({s['name']}) — last activity: {s['last_activity'] or 'never'}")
    else:
        lines.append("- All projects active!")

    lines.extend([
        "",
        "---",
        f"Generated: {datetime.now().isoformat()}",
    ])

    return "\n".join(lines)


# --- Main ---

def main():
    # Parse args
    db_path = None
    print_review = False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--db" and i + 1 < len(args):
            db_path = args[i + 1]
            i += 2
        elif args[i] == "--print":
            print_review = True
            i += 1
        else:
            i += 1

    db_path = find_db(db_path)
    print(f"=== Nightly Review — {datetime.now().isoformat()} ===")
    print(f"Database: {db_path}")

    conn = get_db(db_path)

    stats = get_portfolio_stats(conn)
    accomplishments = get_today_accomplishments(conn)
    blockers = get_blockers(conn)
    stale_projects = get_stale_projects(conn)
    stale_tasks = get_stale_tasks(conn)
    agent_stats = get_agent_stats(conn)
    open_questions = get_open_questions(conn)
    reminders = get_upcoming_reminders(conn)

    conn.close()

    # Generate review
    review = generate_review(stats, accomplishments, blockers, stale_projects,
                             stale_tasks, agent_stats, open_questions, reminders)

    # Save to file
    review_dir = Path(db_path).parent / "nightly-reviews"
    review_dir.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    review_path = review_dir / f"review-{today}.md"
    with open(review_path, "w") as f:
        f.write(review)
    print(f"Review saved to: {review_path}")

    # Print summary
    print(f"\nPortfolio: {stats['active_projects']} active, {stats['done_tasks']} tasks done")
    print(f"Today: {len(accomplishments)} tasks completed")
    print(f"Blockers: {stats['blocked_tasks']}")
    if agent_stats and agent_stats["total_runs"] > 0:
        rate = round(agent_stats["successes"] / agent_stats["total_runs"] * 100, 1)
        print(f"Agents: {agent_stats['total_runs']} runs, {rate}% success, ${agent_stats['total_cost']}")

    if print_review:
        print("\n" + review)

    print("\nDone!")


if __name__ == "__main__":
    main()
