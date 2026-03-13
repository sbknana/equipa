#!/usr/bin/env python3
"""Autoresearch Prompt Optimizer for Project Pombal.

Uses local Ollama (via WSL) to generate OPRO-style prompt mutations based on
agent failure telemetry. Writes improved prompts, backs up originals.

Tiered approach:
  Tier 1: Ollama qwen3.5:9b (free, fast) — initial mutations
  Tier 2: Anthropic Sonnet (if stuck below 80%)
  Tier 3: Anthropic Opus (final polish)

Usage:
    python3 autoresearch_prompts.py                    # Run all agents via Ollama
    python3 autoresearch_prompts.py --role developer   # Single agent
    python3 autoresearch_prompts.py --tier 2           # Use Anthropic Sonnet
    python3 autoresearch_prompts.py --dry-run          # Show proposals, don't write
    python3 autoresearch_prompts.py --rollback         # Restore backups

Copyright 2026, Forgeborn
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

# --- Paths ---
SCRIPT_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = SCRIPT_DIR / "prompts"
BACKUP_DIR = SCRIPT_DIR / ".autoresearch-backups"
THEFORGE_DB = os.environ.get(
    "THEFORGE_DB", str(SCRIPT_DIR / "theforge.db")
)

# Agents to optimize (sorted worst-first)
TARGET_AGENTS = [
    "frontend-designer",  # 30%
    "developer",          # 50%
    "tester",             # 60%
    "security-reviewer",  # 73%
]

# Skip these — already at 100%
SKIP_AGENTS = ["code-reviewer", "planner"]

OLLAMA_MODEL = "qwen3.5:9b"


def get_failure_analysis(db_path: str, role: str) -> dict:
    """Pull failure telemetry from TheForge for a specific agent role."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Overall stats
    stats = conn.execute("""
        SELECT
            COUNT(*) as total_runs,
            SUM(success) as successes,
            ROUND(AVG(success) * 100, 1) as success_pct,
            ROUND(AVG(num_turns), 1) as avg_turns,
            ROUND(AVG(cost_usd), 2) as avg_cost
        FROM agent_runs
        WHERE role = ? AND started_at > '2026-02-20'
    """, (role,)).fetchone()

    # Failure breakdown
    failures = conn.execute("""
        SELECT outcome, COUNT(*) as cnt,
            GROUP_CONCAT(SUBSTR(COALESCE(error_summary, ''), 1, 120), ' | ') as samples
        FROM agent_runs
        WHERE role = ? AND started_at > '2026-02-20' AND success = 0
        GROUP BY outcome ORDER BY cnt DESC
    """, (role,)).fetchall()

    # Success patterns (what works)
    successes = conn.execute("""
        SELECT AVG(num_turns) as avg_turns, MIN(num_turns) as min_turns,
            MAX(num_turns) as max_turns, AVG(cost_usd) as avg_cost
        FROM agent_runs
        WHERE role = ? AND started_at > '2026-02-20' AND success = 1
    """, (role,)).fetchone()

    conn.close()

    return {
        "role": role,
        "total_runs": stats["total_runs"],
        "success_pct": stats["success_pct"],
        "avg_turns": stats["avg_turns"],
        "avg_cost": stats["avg_cost"],
        "failures": [
            {"outcome": f["outcome"], "count": f["cnt"], "samples": f["samples"]}
            for f in failures
        ],
        "success_stats": {
            "avg_turns": successes["avg_turns"],
            "min_turns": successes["min_turns"],
            "max_turns": successes["max_turns"],
        } if successes["avg_turns"] else None,
    }


def build_opro_prompt(current_prompt: str, failure_data: dict) -> str:
    """Build the OPRO meta-prompt for Ollama."""

    failure_summary = ""
    for f in failure_data["failures"]:
        failure_summary += f"  - {f['outcome']}: {f['count']} times\n"
        if f["samples"]:
            # Take first 2 samples
            samples = f["samples"].split(" | ")[:2]
            for s in samples:
                failure_summary += f"    Example: {s.strip()}\n"

    success_info = ""
    if failure_data["success_stats"]:
        s = failure_data["success_stats"]
        success_info = f"""When this agent SUCCEEDS, it typically:
- Uses {s['avg_turns']:.0f} turns on average (range: {s['min_turns']}-{s['max_turns']})
- Starts writing code early and iterates"""

    return f"""/no_think
You are an expert prompt engineer optimizing AI agent prompts for a multi-agent coding system called Project Pombal.

## Current Performance
- Agent role: {failure_data['role']}
- Success rate: {failure_data['success_pct']}% ({failure_data['total_runs']} runs)
- Average turns used: {failure_data['avg_turns']}

## Failure Analysis
{failure_summary}

## Key Insight
The #1 failure mode is "early_terminated" — the agent spends ALL its turns reading/exploring code and NEVER writes any files. The orchestrator kills it after detecting consecutive turns with no file changes.

{success_info}

## Current Prompt (this is what the agent sees)
<current_prompt>
{current_prompt}
</current_prompt>

## Your Task
Rewrite ONLY the agent-specific sections of this prompt to dramatically reduce the early_termination rate. The prompt already has anti-paralysis rules but agents still ignore them.

Rules for your rewrite:
1. Keep the same overall structure and sections
2. Make the "bias for action" rules IMPOSSIBLE to ignore — use stronger language, concrete turn-by-turn mandates, numbered enforcement checkpoints
3. Add a "TURN BUDGET CONTRACT" at the very top that explicitly says "Turn 1: do X. Turn 2: do Y. Turn 3: do Z." — leave ZERO ambiguity
4. Add a "FAILURE MODE AWARENESS" section that describes the exact failure pattern and tells the agent "you WILL be killed if you do this"
5. Remove any language that gives the agent permission to explore extensively
6. Add "MANDATORY CHECKPOINT" rules: "By turn N, you MUST have done X or output RESULT: failed"
7. Keep all the output format sections (RESULT block, etc.) exactly as-is
8. Do NOT add new sections about tools, environment, or TheForge — those come from _common.md
9. Keep it practical and specific — no motivational fluff
10. The rewritten prompt must be the COMPLETE replacement for the current prompt file (minus the _common.md parts which are prepended automatically)

Output ONLY the rewritten prompt. No commentary, no explanation, no markdown fences around it. Just the raw prompt text that will be saved directly to the file."""


def call_ollama(prompt: str, model: str = OLLAMA_MODEL) -> str:
    """Call Ollama via WSL using a temp file to avoid shell escaping issues."""
    import tempfile

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.7,
            "num_predict": 8192,
            "top_p": 0.9,
        }
    }

    # Write payload to a temp file that WSL can read
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False,
                                       dir=tempfile.gettempdir())
    json.dump(payload, tmp)
    tmp.close()
    tmp_path = tmp.name

    # Convert Windows path to WSL path
    # C:\Users\... -> /mnt/c/Users/...
    drive = tmp_path[0].lower()
    wsl_path = f"/mnt/{drive}/{tmp_path[3:].replace(os.sep, '/')}"

    cmd = [
        "wsl", "-e", "bash", "-c",
        f"curl -s http://localhost:11434/api/generate -d @{wsl_path}"
    ]

    print(f"  Calling Ollama ({model})... this may take 2-5 min", flush=True)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600
        )
    finally:
        os.unlink(tmp_path)

    if result.returncode != 0:
        print(f"  ERROR: Ollama call failed: {result.stderr[:200]}")
        return ""

    try:
        data = json.loads(result.stdout)
        response = data.get("response", "")
        # Strip any thinking tags that qwen might produce
        if "<think>" in response:
            # Remove <think>...</think> blocks
            import re as _re
            response = _re.sub(r'<think>.*?</think>', '', response, flags=_re.DOTALL).strip()
        duration = data.get("total_duration", 0) / 1e9
        print(f"  Ollama responded in {duration:.1f}s ({len(response)} chars)")
        return response
    except json.JSONDecodeError:
        print(f"  ERROR: Could not parse Ollama response: {result.stdout[:300]}")
        return ""


def call_anthropic(prompt: str, model: str = "claude-sonnet-4-20250514") -> str:
    """Call Anthropic API for tier 2/3."""
    # Read API key
    api_key = None
    key_file = Path("REDACTED_KEYS_PATH")
    if key_file.exists():
        for line in key_file.read_text().splitlines():
            if "anthropic" in line.lower() and "=" in line:
                api_key = line.split("=", 1)[1].strip()
                break

    if not api_key:
        # Try env
        api_key = os.environ.get("ANTHROPIC_API_KEY")

    if not api_key:
        print("  ERROR: No Anthropic API key found")
        return ""

    payload = json.dumps({
        "model": model,
        "max_tokens": 8192,
        "temperature": 0.7,
        "messages": [{"role": "user", "content": prompt}]
    })

    cmd = [
        "wsl", "-e", "bash", "-c",
        f"""curl -s https://api.anthropic.com/v1/messages \
            -H 'x-api-key: {api_key}' \
            -H 'anthropic-version: 2023-06-01' \
            -H 'content-type: application/json' \
            -d @- <<'PAYLOAD'
{payload}
PAYLOAD"""
    ]

    print(f"  Calling Anthropic ({model})...", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        print(f"  ERROR: Anthropic call failed: {result.stderr[:200]}")
        return ""

    try:
        data = json.loads(result.stdout)
        if "content" in data and data["content"]:
            return data["content"][0].get("text", "")
        if "error" in data:
            print(f"  ERROR: {data['error']}")
        return ""
    except json.JSONDecodeError:
        print(f"  ERROR: Could not parse response")
        return ""


def backup_prompt(role: str) -> Path:
    """Backup current prompt before modification."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    src = PROMPTS_DIR / f"{role}.md"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"{role}_{ts}.md"
    shutil.copy2(src, dst)
    print(f"  Backed up: {dst.name}")
    return dst


def optimize_agent(role: str, tier: int = 1, dry_run: bool = False) -> bool:
    """Run OPRO optimization for a single agent role."""
    print(f"\n{'='*60}")
    print(f"OPTIMIZING: {role}")
    print(f"{'='*60}")

    # Read current prompt
    prompt_file = PROMPTS_DIR / f"{role}.md"
    if not prompt_file.exists():
        print(f"  SKIP: {prompt_file} not found")
        return False

    current_prompt = prompt_file.read_text()
    print(f"  Current prompt: {len(current_prompt)} chars, {len(current_prompt.splitlines())} lines")

    # Get failure data
    failure_data = get_failure_analysis(THEFORGE_DB, role)
    print(f"  Performance: {failure_data['success_pct']}% success ({failure_data['total_runs']} runs)")

    if failure_data["success_pct"] and failure_data["success_pct"] >= 95:
        print(f"  SKIP: Already at {failure_data['success_pct']}% — no optimization needed")
        return False

    # Build OPRO meta-prompt
    meta_prompt = build_opro_prompt(current_prompt, failure_data)

    # Call the appropriate LLM
    if tier == 1:
        new_prompt = call_ollama(meta_prompt)
    elif tier == 2:
        new_prompt = call_anthropic(meta_prompt, "claude-sonnet-4-20250514")
    elif tier == 3:
        new_prompt = call_anthropic(meta_prompt, "claude-opus-4-20250514")
    else:
        print(f"  ERROR: Unknown tier {tier}")
        return False

    if not new_prompt or len(new_prompt) < 200:
        print(f"  ERROR: Generated prompt too short ({len(new_prompt)} chars) — skipping")
        return False

    # Sanity checks
    if "RESULT:" not in new_prompt:
        print("  WARNING: Generated prompt missing RESULT block — may need manual fix")

    # Show diff stats
    old_lines = len(current_prompt.splitlines())
    new_lines = len(new_prompt.splitlines())
    print(f"  Generated prompt: {len(new_prompt)} chars, {new_lines} lines (was {old_lines})")

    if dry_run:
        print(f"\n  [DRY RUN] Would write to {prompt_file}")
        print(f"  First 500 chars of new prompt:")
        print(textwrap.indent(new_prompt[:500], "    "))
        return True

    # Backup and write
    backup_prompt(role)
    prompt_file.write_text(new_prompt)
    print(f"  WRITTEN: {prompt_file}")

    return True


def rollback_all():
    """Restore all prompts from most recent backups."""
    if not BACKUP_DIR.exists():
        print("No backups found.")
        return

    # Find most recent backup for each role
    for role in TARGET_AGENTS:
        backups = sorted(BACKUP_DIR.glob(f"{role}_*.md"), reverse=True)
        if backups:
            src = backups[0]
            dst = PROMPTS_DIR / f"{role}.md"
            shutil.copy2(src, dst)
            print(f"Restored {role} from {src.name}")
        else:
            print(f"No backup for {role}")


def sync_to_claudinator():
    """Rsync updated prompts to Claudinator."""
    print("\nSyncing prompts to Claudinator...")
    cmd = [
        "rsync", "-avz",
        str(PROMPTS_DIR) + "/",
        "user@INTERNAL_HOST:${PROJECT_BASE_DIR}/ProjectPombal/prompts/",
        "-e", "ssh -i ~/.ssh/SSH_KEY_NAME"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode == 0:
        print("  Synced successfully")
    else:
        print(f"  Sync failed: {result.stderr[:200]}")
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description="Autoresearch Prompt Optimizer")
    parser.add_argument("--role", type=str, help="Optimize a single role")
    parser.add_argument("--tier", type=int, default=1, choices=[1, 2, 3],
                       help="LLM tier: 1=Ollama, 2=Sonnet, 3=Opus")
    parser.add_argument("--dry-run", action="store_true", help="Show proposals without writing")
    parser.add_argument("--rollback", action="store_true", help="Restore from backups")
    parser.add_argument("--no-sync", action="store_true", help="Skip rsync to Claudinator")
    args = parser.parse_args()

    if args.rollback:
        rollback_all()
        if not args.no_sync:
            sync_to_claudinator()
        return

    roles = [args.role] if args.role else TARGET_AGENTS
    results = {}

    print(f"Autoresearch Prompt Optimizer — Tier {args.tier}")
    print(f"Targets: {', '.join(roles)}")
    print(f"Ollama model: {OLLAMA_MODEL}" if args.tier == 1 else f"Anthropic tier {args.tier}")
    print()

    for role in roles:
        success = optimize_agent(role, tier=args.tier, dry_run=args.dry_run)
        results[role] = success

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for role, success in results.items():
        status = "OPTIMIZED" if success else "SKIPPED/FAILED"
        print(f"  {role}: {status}")

    if not args.dry_run and any(results.values()) and not args.no_sync:
        sync_to_claudinator()

    print("\nDone. Dispatch test tasks to evaluate the new prompts.")


if __name__ == "__main__":
    main()
