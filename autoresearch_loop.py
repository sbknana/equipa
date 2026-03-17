#!/usr/bin/env python3
"""Autoresearch Loop — Automated prompt optimization for EQUIPA.

ATLAS-style autonomous loop:
  1. Collect recent benchmark metrics from TheForge
  2. Analyze failure patterns
  3. Mutate prompts via Claude CLI (Opus, subscription)
  4. Deploy mutated prompts
  5. Reset project state (git clean) to avoid stale file collisions
  6. Dispatch test tasks via orchestrator
  7. Wait for results
  8. Compare metrics — commit or revert
  9. Loop until target hit or max iterations

Usage:
    python3 autoresearch_loop.py --role developer --target 80
    python3 autoresearch_loop.py --all --target 80
    python3 autoresearch_loop.py --role developer --max-rounds 10
    python3 autoresearch_loop.py --status
    python3 autoresearch_loop.py --rollback developer

Copyright 2026, Forgeborn
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def is_on_claudinator() -> bool:
    """Detect if we're running on the primary server (EQUIPA_BASE exists)."""
    equipa_base = os.environ.get("EQUIPA_BASE", str(Path(__file__).resolve().parent))
    return Path(equipa_base).exists()


# --- Config ---
SCRIPT_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = SCRIPT_DIR / "prompts"
BACKUP_DIR = SCRIPT_DIR / ".autoresearch-backups"
CLAUDINATOR = os.environ.get("SSH_USER", "user") + "@" + os.environ.get("CLAUDINATOR_HOST", "YOUR_HOST")
SSH_KEY = os.path.expanduser(os.environ.get("SSH_KEY_PATH", "~/.ssh/id_ed25519"))
REMOTE_PROMPTS = os.environ.get("EQUIPA_BASE", str(SCRIPT_DIR)) + "/prompts"
REMOTE_ORCHESTRATOR = os.environ.get("EQUIPA_BASE", str(SCRIPT_DIR)) + "/forge_orchestrator.py"
# The orchestrator reads/writes its OWN copy at Equipa/theforge.db.
# We MUST use the same DB so tasks and agent_runs are visible to the orchestrator.
REMOTE_DB = os.environ.get("THEFORGE_DB", str(SCRIPT_DIR / "theforge.db"))
THEFORGE_DB = REMOTE_DB

# Project dirs that need git reset between rounds.
# Value is (path, baseline_ref) — baseline_ref is a tag/commit to reset to.
# This undoes any merged benchmark commits so agents start fresh.
PROJECT_DIRS = {
    39: ("/path/to/your/test-project", "autoresearch-baseline"),
}

# Roles to optimize and their targets
ROLE_TARGETS = {
    "developer": 80,
    "tester": 85,
    "frontend-designer": 80,
    "security-reviewer": 85,
    "debugger": 80,
    "economy-tester": 80,
    "story-tester": 80,
}

# Benchmark metrics window — only count runs from autoresearch tasks
# This avoids historical data drowning out recent benchmark results.
METRICS_WINDOW_RUNS = 15  # Last N benchmark runs per role

# Test task pool — well-scoped tasks across different projects for benchmarking
# Each entry: (project_id, title, description, complexity)
# Tasks are rotated per round to avoid "already exists" collisions.
TEST_TASK_TEMPLATES = {
    "developer": [
        # Pool A — round 1, 4, 7...
        [
            (39, "Benchmark: Add footer component to ForgeArcade",
             "Add a simple footer component to /path/to/your/test-project/src/app/layout.tsx showing 'Built by Forgeborn | © 2026'. Use Tailwind CSS. This is a SIMPLE task — one file edit, ~5 lines of code.",
             "simple"),
            (39, "Benchmark: Add 404 page to ForgeArcade",
             "Create a custom 404 page at /path/to/your/test-project/src/app/not-found.tsx. Show 'Page Not Found' with a link back to home. Use Tailwind CSS dark theme. This is a SIMPLE task — create one new file.",
             "simple"),
            (39, "Benchmark: Add loading spinner to ForgeArcade",
             "Create a loading.tsx file at /path/to/your/test-project/src/app/loading.tsx with a CSS spinner animation. Use Tailwind CSS. This is a SIMPLE task — create one new file.",
             "simple"),
        ],
        # Pool B — round 2, 5, 8...
        [
            (39, "Benchmark: Add health check API to ForgeArcade",
             "Create an API route at /path/to/your/test-project/src/app/api/health/route.ts that returns JSON {status: 'ok', timestamp: Date.now()}. Simple GET handler, ~10 lines.",
             "simple"),
            (39, "Benchmark: Add site metadata to ForgeArcade",
             "Update /path/to/your/test-project/src/app/layout.tsx to export a metadata object with title 'ForgeArcade', description 'Game collection by Forgeborn', and openGraph image placeholder. ~10 lines of code.",
             "simple"),
            (39, "Benchmark: Add robots.txt to ForgeArcade",
             "Create /path/to/your/test-project/src/app/robots.ts that exports a default function returning {rules: {userAgent: '*', allow: '/'}, sitemap: 'https://your-app.example.com/sitemap.xml'}. Next.js metadata API.",
             "simple"),
        ],
        # Pool C — round 3, 6, 9...
        [
            (39, "Benchmark: Add error boundary to ForgeArcade",
             "Create /path/to/your/test-project/src/app/error.tsx as a client component ('use client') that shows 'Something went wrong' with a retry button. Use Tailwind CSS. ~20 lines.",
             "simple"),
            (39, "Benchmark: Add global-error page to ForgeArcade",
             "Create /path/to/your/test-project/src/app/global-error.tsx as a client component that wraps the html/body tags and shows a full-page error state. Use Tailwind. ~15 lines.",
             "simple"),
            (39, "Benchmark: Add sitemap to ForgeArcade",
             "Create /path/to/your/test-project/src/app/sitemap.ts that exports a default function returning an array with one entry: {url: 'https://your-app.example.com', lastModified: new Date()}. Next.js metadata API.",
             "simple"),
        ],
    ],

    # Debugger — needs tasks with known bugs to fix
    "debugger": [
        [
            (39, "Benchmark: Fix broken import in ForgeArcade utils",
             "The file /path/to/your/test-project/src/lib/broken-util.ts has a syntax error. Create this file with a deliberate bug (missing closing bracket in the function), then fix it. The test should verify the build passes with `npx tsc --noEmit`.",
             "simple"),
            (39, "Benchmark: Fix type error in ForgeArcade component",
             "Create /path/to/your/test-project/src/components/BrokenCard.tsx with a React component that has a type mismatch (passing number where string expected). Then fix the type error. Verify with `npx tsc --noEmit`.",
             "simple"),
            (39, "Benchmark: Fix missing return in ForgeArcade helper",
             "Create /path/to/your/test-project/src/lib/score-helper.ts with a function calculateScore that is missing a return statement on one code path. Fix it so all paths return a number. Verify with `npx tsc --noEmit`.",
             "simple"),
        ],
    ],

    # Economy-tester — validates economic/transaction logic
    "economy-tester": [
        [
            (39, "Benchmark: Add DH balance display utility to ForgeArcade",
             "Create /path/to/your/test-project/src/lib/dh-balance.ts with a formatBalance(amount: number) function that formats DragonHoard currency: amounts >= 1000 show as '1.2K DH', amounts >= 1000000 as '1.2M DH', otherwise just 'N DH'. Include edge cases for 0 and negative values. Export the function.",
             "simple"),
            (39, "Benchmark: Add transaction validator to ForgeArcade",
             "Create /path/to/your/test-project/src/lib/transaction-validator.ts with a validateTransaction(from: number, to: number, amount: number) function. Rules: amount must be positive, from !== to, amount must be integer (no fractional DH). Return {valid: boolean, error?: string}.",
             "simple"),
            (39, "Benchmark: Add price calculator to ForgeArcade",
             "Create /path/to/your/test-project/src/lib/price-calc.ts with calculatePrice(basePrice: number, quantity: number, discount?: number) that returns total with optional percentage discount. Clamp discount to 0-100. Return 0 if quantity <= 0.",
             "simple"),
        ],
    ],

    # Story-tester — validates narrative/content logic
    "story-tester": [
        [
            (39, "Benchmark: Add dialogue formatter to ForgeArcade",
             "Create /path/to/your/test-project/src/lib/dialogue.ts with a formatDialogue(speaker: string, text: string, mood?: 'happy'|'angry'|'neutral') function. Returns formatted string like '[Speaker (mood)]: text'. Default mood is 'neutral'. Handle empty speaker/text gracefully.",
             "simple"),
            (39, "Benchmark: Add quest tracker types to ForgeArcade",
             "Create /path/to/your/test-project/src/lib/quest-types.ts with TypeScript interfaces: Quest {id, title, description, status: 'active'|'completed'|'failed'}, QuestStep {id, questId, description, completed: boolean}, and a QuestLog type that is Quest & {steps: QuestStep[]}.",
             "simple"),
            (39, "Benchmark: Add lore entry component to ForgeArcade",
             "Create /path/to/your/test-project/src/components/LoreEntry.tsx — a React component that takes props {title: string, content: string, category: string, discovered: boolean}. If not discovered, show title with '???' for content. Use Tailwind CSS with dark theme.",
             "simple"),
        ],
    ],
}

MAX_WAIT_SECONDS = 1800  # 30 min max wait for tasks
POLL_INTERVAL = 30  # Check every 30s


def ssh_cmd(cmd: str, timeout: int = 60) -> str:
    """Run a command on Claudinator (via SSH or locally if already there)."""
    if is_on_claudinator():
        full_cmd = ["bash", "-c", cmd]
    else:
        full_cmd = ["ssh", "-i", SSH_KEY, CLAUDINATOR, cmd]
    try:
        result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    except Exception as e:
        return f"ERROR: {e}"


def db_query(sql: str, timeout: int = 30) -> list[dict]:
    """Run a SQL query against TheForge DB (always on Claudinator).

    Returns list of dicts. Uses sqlite3 directly if on Claudinator,
    or SSH + sqlite3 CLI if remote.
    """
    if is_on_claudinator():
        conn = sqlite3.connect(THEFORGE_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    else:
        escaped_sql = sql.replace("'", "'\\''")
        cmd = f"sqlite3 -json '{REMOTE_DB}' '{escaped_sql}'"
        raw = ssh_cmd(cmd, timeout=timeout)
        if not raw or raw.startswith("ERROR") or raw == "TIMEOUT":
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []


def db_execute(sql: str, timeout: int = 30) -> bool:
    """Run an INSERT/UPDATE/DELETE against TheForge DB (always on Claudinator)."""
    if is_on_claudinator():
        conn = sqlite3.connect(THEFORGE_DB)
        conn.execute(sql)
        conn.commit()
        conn.close()
        return True
    else:
        escaped_sql = sql.replace("'", "'\\''")
        cmd = f"sqlite3 '{REMOTE_DB}' '{escaped_sql}'"
        result = ssh_cmd(cmd, timeout=timeout)
        return not result.startswith("ERROR")


def get_metrics(role: str) -> dict:
    """Get recent benchmark metrics for a role.

    Uses a sliding window of the last N runs to avoid historical data
    drowning out recent benchmark improvements.
    """
    rows = db_query(f"""
        SELECT COUNT(*) as runs,
               SUM(success) as successes,
               ROUND(AVG(success) * 100, 1) as success_pct,
               ROUND(AVG(num_turns), 1) as avg_turns,
               ROUND(AVG(cost_usd), 2) as avg_cost
        FROM (
            SELECT success, num_turns, cost_usd
            FROM agent_runs
            WHERE role = '{role}'
            ORDER BY id DESC
            LIMIT {METRICS_WINDOW_RUNS}
        )
    """)
    if not rows or not rows[0].get("runs"):
        return {"runs": 0, "success_pct": 0, "avg_turns": 0}
    return rows[0]


def get_failure_analysis(role: str) -> str:
    """Get failure analysis from recent runs for feeding into mutation prompt."""
    failures = db_query(f"""
        SELECT outcome, COUNT(*) as cnt,
            GROUP_CONCAT(SUBSTR(COALESCE(error_summary, ''), 1, 100), ' | ') as samples
        FROM (
            SELECT outcome, error_summary, success
            FROM agent_runs
            WHERE role = '{role}'
            ORDER BY id DESC
            LIMIT {METRICS_WINDOW_RUNS}
        )
        WHERE success = 0
        GROUP BY outcome ORDER BY cnt DESC LIMIT 5
    """)

    lines = []
    for f in failures:
        lines.append(f"  - {f['outcome']}: {f['cnt']} times")
        if f.get('samples'):
            for s in f['samples'].split(' | ')[:2]:
                if s.strip():
                    lines.append(f"    Example: {s.strip()[:80]}")
    return "\n".join(lines) if lines else "  (no recent failures)"


def reset_project_state(project_id: int):
    """Hard reset project to baseline state between rounds.

    Resets to a tagged baseline commit, undoing any merged benchmark work.
    This prevents agents from seeing files created by previous rounds
    and incorrectly reporting 'already exists / no changes needed'.
    Also prunes stale worktree branches.
    """
    entry = PROJECT_DIRS.get(project_id)
    if not entry:
        return
    project_dir, baseline_ref = entry

    print(f"  Resetting {project_dir} to {baseline_ref}...", flush=True)
    reset_cmd = (
        f"cd {project_dir} && "
        # Remove stale worktrees from previous rounds
        f"git worktree prune 2>/dev/null; "
        # Hard reset to baseline (undoes merged benchmark commits)
        f"git reset --hard {baseline_ref} 2>/dev/null; "
        # Clean untracked files (but keep node_modules, .next)
        f"git clean -fd --exclude=node_modules --exclude=.next 2>/dev/null; "
        # Delete leftover benchmark branches
        f"git branch | grep forge-task | xargs -r git branch -D 2>/dev/null; "
        f"echo RESET_OK"
    )
    result = ssh_cmd(reset_cmd, timeout=30)
    if "RESET_OK" in result:
        print(f"  Reset to baseline.", flush=True)
    else:
        print(f"  WARNING: Reset may have failed: {result[:100]}", flush=True)


def backup_prompt(role: str) -> Path:
    """Backup current prompt."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    src = PROMPTS_DIR / f"{role}.md"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"{role}_{ts}.md"
    shutil.copy2(src, dst)
    return dst


def mutate_prompt(role: str, current_prompt: str, failure_text: str, metrics: dict) -> str:
    """Generate a mutated prompt using Claude CLI (Opus, subscription-based).

    Always uses Opus — it produces tighter, smarter prompts than Sonnet
    and avoids the prompt bloat problem Sonnet exhibited.
    """
    meta_prompt = f"""You are an expert prompt engineer. You are optimizing an AI agent prompt through automated search.

CURRENT METRICS for role "{role}" (last {METRICS_WINDOW_RUNS} runs):
- Success rate: {metrics.get('success_pct', 0)}%
- Total runs: {metrics.get('runs', 0)}
- Average turns: {metrics.get('avg_turns', 0)}

FAILURE PATTERNS (recent):
{failure_text}

CURRENT PROMPT:
<prompt>
{current_prompt}
</prompt>

TASK: Rewrite this prompt to increase the success rate. Based on the failure patterns:
- If early_terminated is the top failure: make action mandates stronger and earlier
- If timeout is the top failure: add instructions for smaller, faster edits
- If tester_blocked: improve test-writing instructions
- If cycles_exhausted: improve first-attempt accuracy
- If early_completed_no_changes: add instruction to ALWAYS write code even if file exists (overwrite it)

Rules:
1. Keep the same overall structure and output format sections
2. Make ONE targeted change based on the top failure pattern — don't rewrite everything
3. The change should be specific and testable
4. Keep the prompt CONCISE — under 8000 chars. Remove fluff, redundancy, and over-explanation.
   Agents respond better to short, clear directives than walls of text.
5. Output ONLY the complete rewritten prompt — no commentary

Output the raw prompt text only."""

    print(f"  Calling Claude CLI (opus, subscription)...", flush=True)

    # Write meta_prompt to a temp file to avoid shell quoting issues
    import tempfile
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                      dir="/tmp" if is_on_claudinator() else None)
    tmp.write(meta_prompt)
    tmp.close()
    tmp_path = tmp.name

    try:
        if is_on_claudinator():
            result = subprocess.run(
                ["bash", "-c", f'claude --print --model opus < "{tmp_path}"'],
                capture_output=True, text=True, timeout=300
            )
        else:
            # Running on Windows — SCP the prompt file, then SSH to run claude
            remote_tmp = f"/tmp/autoresearch_prompt_{role}.txt"
            subprocess.run(
                ["scp", "-i", SSH_KEY, tmp_path, f"{CLAUDINATOR}:{remote_tmp}"],
                capture_output=True, timeout=30
            )
            result = subprocess.run(
                ["ssh", "-i", SSH_KEY, CLAUDINATOR,
                 f'claude --print --model opus < "{remote_tmp}"'],
                capture_output=True, text=True, timeout=300
            )

        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        else:
            print(f"  ERROR: Claude CLI returned code {result.returncode}")
            if result.stderr:
                print(f"  STDERR: {result.stderr[:200]}")
            return ""
    except subprocess.TimeoutExpired:
        print(f"  ERROR: Claude CLI timed out (300s)")
        return ""
    except Exception as e:
        print(f"  ERROR: Claude CLI failed: {e}")
        return ""
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def deploy_prompt(role: str, content: str):
    """Write prompt to file (locally if on Claudinator, or sync via SSH)."""
    prompt_file = PROMPTS_DIR / f"{role}.md"
    prompt_file.write_text(content)

    if not is_on_claudinator():
        cmd = [
            "scp", "-i", SSH_KEY,
            str(prompt_file),
            f"{CLAUDINATOR}:{REMOTE_PROMPTS}/{role}.md",
        ]
        subprocess.run(cmd, capture_output=True, timeout=30)


def create_test_tasks(role: str, round_num: int) -> list:
    """Create benchmark test tasks in TheForge and return their IDs."""
    task_ids = []
    pools = TEST_TASK_TEMPLATES.get(role, TEST_TASK_TEMPLATES["developer"])
    # Rotate through pools: round 1 -> pool 0, round 2 -> pool 1, etc.
    pool_idx = (round_num - 1) % len(pools)
    templates = pools[pool_idx]

    for project_id, title, desc, complexity in templates:
        tagged_title = f"[AR-R{round_num}] {title}"
        safe_desc = desc.replace("'", "''")
        safe_title = tagged_title.replace("'", "''")
        db_execute(f"""INSERT INTO tasks (project_id, title, description, status, priority, complexity)
               VALUES ({project_id}, '{safe_title}', '{safe_desc}', 'todo', 'medium', '{complexity}')""")
        rows = db_query("SELECT MAX(id) as id FROM tasks")
        if rows:
            task_ids.append(rows[0]["id"])

    return task_ids


def dispatch_tasks(task_ids: list) -> int:
    """Dispatch tasks via orchestrator on Claudinator. Returns PID."""
    id_range = f"{min(task_ids)}-{max(task_ids)}"
    cmd = (
        f"nohup python3 -u {REMOTE_ORCHESTRATOR} "
        f"--tasks {id_range} --dev-test -y "
        f"> /tmp/forge-autoresearch-{id_range}.log 2>&1 & echo $!"
    )
    pid = ssh_cmd(cmd, timeout=10)
    return int(pid) if pid.isdigit() else 0


def wait_for_tasks(task_ids: list) -> dict:
    """Poll TheForge until all tasks are done or timeout."""
    id_list = ','.join(map(str, task_ids))
    start = time.time()

    while time.time() - start < MAX_WAIT_SECONDS:
        rows = db_query(f"SELECT id, status FROM tasks WHERE id IN ({id_list})")
        statuses = {r["id"]: r["status"] for r in rows}
        pending = [tid for tid, s in statuses.items() if s in ('todo', 'in_progress')]

        if not pending:
            break

        print(f"  Waiting... {len(pending)}/{len(task_ids)} tasks pending "
              f"({int(time.time() - start)}s elapsed)", flush=True)
        time.sleep(POLL_INTERVAL)

    # Collect results from agent_runs
    results = db_query(f"""
        SELECT task_id, role, num_turns, outcome, success, cost_usd
        FROM agent_runs
        WHERE task_id IN ({id_list})
        ORDER BY id
    """)

    successes = sum(1 for r in results if r.get("success") == 1)
    total = len(results) if results else len(task_ids)

    return {
        "total": total,
        "successes": successes,
        "success_pct": round(successes / total * 100, 1) if total > 0 else 0,
        "results": results,
    }


def rollback_prompt(role: str):
    """Restore prompt from most recent backup."""
    backups = sorted(BACKUP_DIR.glob(f"{role}_*.md"), reverse=True)
    if not backups:
        print(f"No backups found for {role}")
        return
    src = backups[0]
    dst = PROMPTS_DIR / f"{role}.md"
    shutil.copy2(src, dst)
    deploy_prompt(role, dst.read_text())
    print(f"Rolled back {role} from {src.name}")


def run_optimization_loop(role: str, target_pct: int, max_rounds: int = 10):
    """Main autoresearch loop for a single role."""
    print(f"\n{'='*60}")
    print(f"AUTORESEARCH: {role} (target: {target_pct}%, model: opus)")
    print(f"{'='*60}")

    # Track benchmark results across this run for comparison
    round_results = []

    for round_num in range(1, max_rounds + 1):
        print(f"\n--- Round {round_num}/{max_rounds} ---")

        # 1. Collect recent metrics (sliding window)
        metrics = get_metrics(role)
        current_pct = metrics.get("success_pct", 0) or 0
        print(f"  Recent: {current_pct}% success (last {metrics.get('runs', 0)} runs)")

        if current_pct >= target_pct and metrics.get("runs", 0) >= 6:
            print(f"  TARGET REACHED! {current_pct}% >= {target_pct}%")
            break

        # 2. Analyze failures
        failure_text = get_failure_analysis(role)
        print(f"  Failures analyzed")

        # 3. Read current prompt
        prompt_file = PROMPTS_DIR / f"{role}.md"
        current_prompt = prompt_file.read_text()

        # 4. Backup and mutate (always Opus)
        backup_prompt(role)
        new_prompt = mutate_prompt(role, current_prompt, failure_text, metrics)

        if not new_prompt or len(new_prompt) < 200:
            print(f"  Mutation failed (empty or too short). Skipping round.")
            continue

        # 5. Deploy
        deploy_prompt(role, new_prompt)
        print(f"  Deployed mutated prompt ({len(new_prompt)} chars)")

        # 6. Reset project state — hard reset to baseline before each round
        pools = TEST_TASK_TEMPLATES.get(role, TEST_TASK_TEMPLATES["developer"])
        pool_idx = (round_num - 1) % len(pools)
        project_ids_in_pool = set(t[0] for t in pools[pool_idx])
        for proj_id in project_ids_in_pool:
            reset_project_state(proj_id)

        # 7. Create and dispatch test tasks
        task_ids = create_test_tasks(role, round_num)
        print(f"  Created test tasks: {task_ids}")

        pid = dispatch_tasks(task_ids)
        if not pid:
            print(f"  Failed to dispatch tasks. Reverting.")
            rollback_prompt(role)
            continue
        print(f"  Dispatched (PID {pid})")

        # 8. Wait for results
        print(f"  Waiting for results (max {MAX_WAIT_SECONDS}s)...")
        results = wait_for_tasks(task_ids)
        new_pct = results["success_pct"]
        round_results.append(new_pct)

        print(f"\n  Round {round_num} Results: {new_pct}% ({results['successes']}/{results['total']})")
        for r in results.get("results", []):
            status = "PASS" if r.get("success") else "FAIL"
            print(f"    Task {r.get('task_id')}: {status} ({r.get('outcome')}, {r.get('num_turns')} turns)")

        # 9. Commit or revert based on round results
        if new_pct >= target_pct:
            print(f"  HIT TARGET: {new_pct}% >= {target_pct}%. Keeping mutation.")
        elif new_pct > 0 and (not round_results[:-1] or new_pct >= max(round_results[:-1], default=0)):
            print(f"  KEEPING: {new_pct}% (best or equal this run).")
        elif new_pct == 0:
            print(f"  ZERO PASS: Reverting mutation.")
            rollback_prompt(role)
        else:
            avg_recent = sum(round_results[-3:]) / len(round_results[-3:]) if len(round_results) >= 3 else new_pct
            if avg_recent >= 60:
                print(f"  ACCEPTABLE: {new_pct}% this round, {avg_recent:.0f}% avg last 3. Keeping.")
            else:
                print(f"  REGRESSION: {new_pct}% this round, {avg_recent:.0f}% avg. Reverting.")
                rollback_prompt(role)

    # Final status
    final_metrics = get_metrics(role)
    avg_benchmark = sum(round_results) / len(round_results) if round_results else 0
    print(f"\n  FINAL: {role}")
    print(f"    Recent DB metric: {final_metrics.get('success_pct', 0)}% (last {METRICS_WINDOW_RUNS} runs)")
    print(f"    This run avg: {avg_benchmark:.1f}% across {len(round_results)} rounds")
    print(f"    Round scores: {round_results}")


def show_status():
    """Show current metrics for all roles (recent window)."""
    rows = db_query(f"""
        SELECT role, runs, successes, success_pct, avg_turns FROM (
            SELECT role,
                   COUNT(*) as runs,
                   SUM(success) as successes,
                   ROUND(AVG(success) * 100, 1) as success_pct,
                   ROUND(AVG(num_turns), 1) as avg_turns
            FROM (
                SELECT role, success, num_turns,
                       ROW_NUMBER() OVER (PARTITION BY role ORDER BY id DESC) as rn
                FROM agent_runs
            )
            WHERE rn <= {METRICS_WINDOW_RUNS}
            GROUP BY role
        )
        ORDER BY success_pct DESC
    """)

    if not rows:
        # Fallback if window function not supported
        rows = db_query("""
            SELECT role,
                   COUNT(*) as runs,
                   SUM(success) as successes,
                   ROUND(AVG(success) * 100, 1) as success_pct,
                   ROUND(AVG(num_turns), 1) as avg_turns
            FROM agent_runs
            WHERE started_at > '2026-02-20'
            GROUP BY role
            ORDER BY success_pct DESC
        """)

    print(f"\n{'Role':<22} {'Runs':>5} {'Success':>8} {'Avg Turns':>10} {'Target':>8}")
    print("-" * 58)
    for r in rows:
        target = ROLE_TARGETS.get(r["role"], "—")
        pct = f"{r['success_pct']}%"
        met = " ✓" if isinstance(target, int) and r["success_pct"] >= target else ""
        print(f"{r['role']:<22} {r['runs']:>5} {pct:>8} {r['avg_turns']:>10} {str(target)+('%' if isinstance(target, int) else ''):>8}{met}")


def main():
    parser = argparse.ArgumentParser(description="Autoresearch Loop — Opus-powered prompt optimization")
    parser.add_argument("--role", type=str, help="Optimize a single role")
    parser.add_argument("--all", action="store_true", help="Optimize all underperforming roles")
    parser.add_argument("--target", type=int, default=80, help="Target success %% (default: 80)")
    parser.add_argument("--max-rounds", type=int, default=10, help="Max optimization rounds")
    parser.add_argument("--status", action="store_true", help="Show current metrics")
    parser.add_argument("--rollback", type=str, help="Rollback a role to last backup")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.rollback:
        rollback_prompt(args.rollback)
        return

    if args.role:
        run_optimization_loop(args.role, args.target, args.max_rounds)
    elif args.all:
        for role, default_target in ROLE_TARGETS.items():
            target = args.target or default_target
            run_optimization_loop(role, target, args.max_rounds)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

