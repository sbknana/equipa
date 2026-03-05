#!/usr/bin/env python3
"""ForgeSmith SIMBA — Targeted rule generation from failure patterns.

SIMBA (Systematic Identification of Mistakes and Behavioral Adjustments) analyzes
agent_episodes for high-variance tasks and uses Claude to generate specific
improvement rules that go beyond the generic pattern-matching in extract_lessons().

Pipeline position: runs after evaluate, before propose (Phase 2.75).

Usage:
    # As part of ForgeSmith pipeline (integrated in run_full)
    python3 forgesmith.py --auto

    # Standalone for testing
    python3 forgesmith_simba.py --role developer
    python3 forgesmith_simba.py --dry-run
    python3 forgesmith_simba.py --prune
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# H1 fix: Whitelist valid model identifiers to prevent command injection via --model
ALLOWED_MODELS = frozenset({
    "sonnet", "opus", "haiku",
    "claude-sonnet-4-20250514", "claude-opus-4-20250514",
    "claude-haiku-4-5-20251001",
    "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022",
})

# --- Paths ---

SCRIPT_DIR = Path(__file__).resolve().parent
THEFORGE_DB = os.environ.get(
    "THEFORGE_DB",
    str(Path(__file__).resolve().parent / "theforge.db"),
)

# --- Constants ---

MAX_RULES_PER_ROLE = 3
PRUNE_INJECT_THRESHOLD = 50
MIN_EPISODES_FOR_ANALYSIS = 3
LOW_Q_THRESHOLD = 0.3


# --- DB Helpers ---

def get_db(write=False):
    """Connect to TheForge SQLite database."""
    db_path = os.environ.get("THEFORGE_DB", THEFORGE_DB)
    if write:
        conn = sqlite3.connect(db_path)
    else:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def log(msg):
    """Print timestamped message."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [SIMBA] {msg}")


# ============================================================
# STEP 1: Identify high-variance tasks and failure patterns
# ============================================================

def find_high_variance_episodes(lookback_days=30):
    """Find roles with mixed outcomes on similar tasks.

    High-variance = same role has both successes AND failures.
    These are the most useful for rule generation because we can
    contrast what worked vs what didn't.

    Returns dict keyed by role with success and failure episodes.
    """
    conn = get_db()
    rows = conn.execute(
        """SELECT id, task_id, role, task_type, project_id,
                  approach_summary, turns_used, outcome, error_patterns,
                  reflection, q_value, created_at
           FROM agent_episodes
           WHERE created_at >= datetime('now', ?)
             AND reflection IS NOT NULL AND reflection != ''
           ORDER BY role, outcome""",
        (f"-{lookback_days} days",),
    ).fetchall()
    conn.close()

    episodes = [dict(r) for r in rows]

    # Group by role
    by_role = {}
    for ep in episodes:
        role = ep["role"]
        if role not in by_role:
            by_role[role] = {"successes": [], "failures": []}

        if ep["outcome"] in ("success", "tests_passed", "no_tests"):
            by_role[role]["successes"].append(ep)
        elif ep["outcome"] in ("early_terminated", "blocked", "cycles_exhausted",
                                "developer_max_turns"):
            by_role[role]["failures"].append(ep)

    # Only keep roles with BOTH successes and failures (high variance)
    high_variance = {}
    for role, groups in by_role.items():
        if groups["successes"] and groups["failures"]:
            high_variance[role] = groups

    return high_variance


def find_hardest_cases(lookback_days=30):
    """Find the hardest failure cases: early_terminated with q_value < 0.3.

    These represent tasks where agents struggled the most and could benefit
    from very specific rules.
    """
    conn = get_db()
    rows = conn.execute(
        """SELECT id, task_id, role, task_type, project_id,
                  approach_summary, turns_used, outcome, error_patterns,
                  reflection, q_value, created_at
           FROM agent_episodes
           WHERE created_at >= datetime('now', ?)
             AND outcome = 'early_terminated'
             AND q_value < ?
             AND reflection IS NOT NULL AND reflection != ''
           ORDER BY q_value ASC""",
        (f"-{lookback_days} days", LOW_Q_THRESHOLD),
    ).fetchall()
    conn.close()

    return [dict(r) for r in rows]


def get_existing_simba_rules(role=None):
    """Get existing SIMBA-generated rules to avoid duplicates."""
    conn = get_db()
    conditions = ["source = 'simba_generated'", "active = 1"]
    params = []
    if role:
        conditions.append("role = ?")
        params.append(role)

    where = " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT * FROM lessons_learned WHERE {where}",
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
# STEP 2: Build analysis prompt and call Claude
# ============================================================

def build_simba_prompt(role, successes, failures, hardest_cases, existing_rules):
    """Build a prompt for Claude to generate specific improvement rules.

    Contrasts successful vs failed episodes for the same role to identify
    what differentiates them, then generates targeted rules.
    """
    # Format success episodes (up to 5)
    success_text = ""
    for ep in successes[:5]:
        success_text += f"""
- Task #{ep['task_id']} ({ep['outcome']}, {ep['turns_used']} turns, q={ep['q_value']:.1f}):
  Approach: {(ep.get('approach_summary') or 'N/A')[:200]}
  Reflection: {(ep['reflection'] or 'N/A')[:300]}
"""

    # Format failure episodes (up to 8)
    failure_text = ""
    for ep in failures[:8]:
        failure_text += f"""
- Task #{ep['task_id']} ({ep['outcome']}, {ep['turns_used']} turns, q={ep['q_value']:.1f}):
  Error: {(ep.get('error_patterns') or 'N/A')[:200]}
  Reflection: {(ep['reflection'] or 'N/A')[:300]}
"""

    # Format hardest cases (up to 5)
    hardest_text = ""
    for ep in hardest_cases[:5]:
        hardest_text += f"""
- Task #{ep['task_id']} ({ep['turns_used']} turns, q={ep['q_value']:.2f}):
  Error: {(ep.get('error_patterns') or 'N/A')[:200]}
  Reflection: {(ep['reflection'] or 'N/A')[:300]}
"""

    # Format existing rules to avoid duplicates
    existing_text = ""
    if existing_rules:
        existing_text = "\n## Existing Rules (DO NOT duplicate these)\n"
        for rule in existing_rules:
            existing_text += f"- {rule['lesson']}\n"

    prompt = f"""You are analyzing agent performance data to generate specific improvement rules.

## Role: {role}

## Successful Episodes (what worked)
{success_text if success_text else "No successful episodes recorded."}

## Failed Episodes (what didn't work)
{failure_text if failure_text else "No failed episodes recorded."}

## Hardest Cases (q_value < 0.3, most struggling)
{hardest_text if hardest_text else "No hardest cases found."}
{existing_text}

## Task

Analyze the contrast between successes and failures. Identify specific, actionable patterns that distinguish them. Then generate up to {MAX_RULES_PER_ROLE} new rules.

Requirements for each rule:
1. MUST be 1-2 sentences, max 200 characters
2. MUST be specific and actionable (not generic advice like "plan better")
3. MUST reference concrete behaviors observed in the data (e.g., "When exploring a codebase, use parallel Read calls instead of sequential ones to avoid burning turns")
4. MUST NOT duplicate any existing rules listed above
5. Each rule MUST include an error_type from: timeout, max_turns, early_terminated, agent_error, test_failure

Respond with ONLY a JSON array of objects, each with:
- "rule": the rule text (1-2 sentences, max 200 chars)
- "error_type": which failure pattern this addresses
- "rationale": why this rule would help (1 sentence)

Example:
[
  {{
    "rule": "When task description mentions 'sync' or 'diff', limit exploration to 10 turns then start writing code with what you know.",
    "error_type": "early_terminated",
    "rationale": "Sync tasks caused 40+ turn exploration loops in 4 of 5 failed episodes."
  }}
]
"""
    return prompt


def call_claude_for_rules(prompt, cfg=None):
    """Call Claude CLI to generate SIMBA rules.

    Uses the same subprocess pattern as OPRO's call_claude_for_proposals.
    """
    simba_cfg = (cfg or {}).get("simba", {})
    model = simba_cfg.get("model", "sonnet")
    timeout = simba_cfg.get("timeout_seconds", 120)

    # H1 fix: Validate model against whitelist to prevent command injection
    if model not in ALLOWED_MODELS:
        log(f"ERROR: Model '{model}' not in ALLOWED_MODELS. "
            f"Valid models: {sorted(ALLOWED_MODELS)}")
        return None

    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--max-turns", "2",
        "--no-session-persistence",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        log("Claude call timed out")
        return None
    except FileNotFoundError:
        log("'claude' command not found")
        return None

    if result.returncode != 0:
        log(f"Claude returned non-zero exit code: {result.returncode}")
        if result.stderr:
            log(f"stderr: {result.stderr[:200]}")
        return None

    raw = result.stdout.strip()
    if not raw:
        log("Empty response from Claude")
        return None

    # Parse JSON — Claude --output-format json wraps in {"result": "..."}
    try:
        outer = json.loads(raw)
        if isinstance(outer, dict) and "result" in outer:
            inner = outer["result"]
        else:
            inner = raw
    except json.JSONDecodeError:
        inner = raw

    # Extract JSON array from text (may include markdown fences)
    if isinstance(inner, str):
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", inner.strip())
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        try:
            rules = json.loads(cleaned)
        except json.JSONDecodeError:
            log(f"Failed to parse rules JSON: {cleaned[:200]}")
            return None
    elif isinstance(inner, list):
        rules = inner
    else:
        log(f"Unexpected response format: {type(inner)}")
        return None

    if not isinstance(rules, list):
        log(f"Expected list of rules, got {type(rules)}")
        return None

    return rules


# ============================================================
# STEP 3: Validate and store rules
# ============================================================

def validate_rule(rule, existing_rules):
    """Validate a generated rule before storing.

    Returns (is_valid, reason).
    """
    if not isinstance(rule, dict):
        return False, "not a dict"

    text = rule.get("rule", "").strip()
    if not text:
        return False, "empty rule text"

    if len(text) > 250:
        return False, f"too long ({len(text)} chars, max 250)"

    if len(text) < 20:
        return False, f"too short ({len(text)} chars, min 20)"

    error_type = rule.get("error_type", "")
    valid_types = {"timeout", "max_turns", "early_terminated", "agent_error",
                   "test_failure"}
    if error_type not in valid_types:
        return False, f"invalid error_type: {error_type}"

    # Check for duplicates against existing rules
    text_lower = text.lower()
    for existing in existing_rules:
        existing_lower = existing["lesson"].lower()
        # Simple similarity check: if >60% of words overlap, it's a duplicate
        text_words = set(text_lower.split())
        existing_words = set(existing_lower.split())
        if text_words and existing_words:
            overlap = len(text_words & existing_words) / min(len(text_words),
                                                             len(existing_words))
            if overlap > 0.6:
                return False, f"too similar to existing rule #{existing['id']}"

    return True, "ok"


def store_rules(role, rules, dry_run=False):
    """Store validated rules in lessons_learned table.

    Returns list of stored rule dicts.
    """
    existing = get_existing_simba_rules(role)
    # Also get ALL existing lessons for this role (to check duplicates broadly)
    conn_ro = get_db()
    all_existing = conn_ro.execute(
        """SELECT id, lesson FROM lessons_learned
           WHERE active = 1 AND (role = ? OR role IS NULL)""",
        (role,),
    ).fetchall()
    conn_ro.close()
    all_existing = [dict(r) for r in all_existing]

    stored = []
    for rule in rules[:MAX_RULES_PER_ROLE]:
        is_valid, reason = validate_rule(rule, all_existing)
        if not is_valid:
            log(f"  Rejected rule: {reason} — {rule.get('rule', '')[:80]}")
            continue

        text = rule["rule"].strip()
        error_type = rule["error_type"]
        rationale = rule.get("rationale", "")

        if dry_run:
            log(f"  [DRY RUN] Would store: {text}")
            stored.append({"rule": text, "error_type": error_type, "stored": False})
            continue

        # Generate unique signature from rule text to prevent collision
        sig_hash = hashlib.sha256(text.encode()).hexdigest()[:12]
        signature = f"simba:{error_type}:{role}:{sig_hash}"

        conn = get_db(write=True)
        try:
            conn.execute(
                """INSERT INTO lessons_learned
                   (role, error_type, error_signature, lesson, source, times_seen)
                   VALUES (?, ?, ?, ?, 'simba_generated', 1)""",
                (role, error_type, signature, text),
            )
            conn.commit()
            log(f"  Stored rule for [{role}]: {text}")
            stored.append({"rule": text, "error_type": error_type, "stored": True})
        finally:
            conn.close()

    return stored


# ============================================================
# STEP 4: Evaluate rule effectiveness
# ============================================================

def evaluate_simba_rules():
    """Score SIMBA rules by comparing success rates before/after rule creation.

    For each active SIMBA rule with times_injected >= 10:
    - Compute success rate for that role in episodes BEFORE the rule was created
    - Compute success rate for that role in episodes AFTER the rule was created
    - effectiveness_score = (after_rate - before_rate), range [-1.0, 1.0]

    This ensures rules are evaluated before pruning, so effective rules
    are not pruned just because effectiveness_score was NULL.
    """
    conn = get_db(write=True)
    rules = conn.execute(
        """SELECT id, role, error_type, created_at, times_injected
           FROM lessons_learned
           WHERE source = 'simba_generated'
             AND active = 1
             AND times_injected >= 10"""
    ).fetchall()

    evaluated = 0
    for rule in rules:
        rule = dict(rule)
        role = rule["role"]
        created_at = rule["created_at"]

        if not role or not created_at:
            continue

        # Success rate BEFORE rule was created (up to 30 days prior)
        before = conn.execute(
            """SELECT
                 COUNT(*) as total,
                 SUM(CASE WHEN outcome IN ('success', 'tests_passed', 'no_tests')
                      THEN 1 ELSE 0 END) as successes
               FROM agent_episodes
               WHERE role = ?
                 AND created_at < ?
                 AND created_at >= datetime(?, '-30 days')""",
            (role, created_at, created_at),
        ).fetchone()

        # Success rate AFTER rule was created
        after = conn.execute(
            """SELECT
                 COUNT(*) as total,
                 SUM(CASE WHEN outcome IN ('success', 'tests_passed', 'no_tests')
                      THEN 1 ELSE 0 END) as successes
               FROM agent_episodes
               WHERE role = ?
                 AND created_at >= ?""",
            (role, created_at),
        ).fetchone()

        before_total = before["total"] if before else 0
        after_total = after["total"] if after else 0

        # Need minimum sample sizes for a meaningful comparison
        if before_total < 3 or after_total < 3:
            continue

        before_rate = before["successes"] / before_total
        after_rate = after["successes"] / after_total
        score = round(after_rate - before_rate, 3)

        conn.execute(
            """UPDATE lessons_learned
               SET effectiveness_score = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (score, rule["id"]),
        )
        evaluated += 1
        log(f"  Rule #{rule['id']}: before={before_rate:.2f} ({before_total} eps), "
            f"after={after_rate:.2f} ({after_total} eps), score={score:+.3f}")

    if evaluated:
        conn.commit()
    conn.close()

    return evaluated


# ============================================================
# STEP 5: Prune ineffective rules
# ============================================================

def prune_stale_rules(dry_run=False):
    """Prune SIMBA rules with high inject_count but no effectiveness improvement.

    A rule is pruned if:
    - times_injected > PRUNE_INJECT_THRESHOLD (shown 50+ times)
    - effectiveness_score is NULL (never evaluated) or <= 0

    This prevents rule bloat from accumulating over time.
    """
    conn = get_db(write=not dry_run)
    rows = conn.execute(
        """SELECT id, role, lesson, times_injected, effectiveness_score
           FROM lessons_learned
           WHERE source = 'simba_generated'
             AND active = 1
             AND times_injected > ?
             AND (effectiveness_score IS NULL OR effectiveness_score <= 0)""",
        (PRUNE_INJECT_THRESHOLD,),
    ).fetchall()

    pruned = []
    for r in rows:
        rule = dict(r)
        if dry_run:
            log(f"  [DRY RUN] Would prune rule #{rule['id']}: {rule['lesson'][:80]}")
        else:
            conn.execute(
                """UPDATE lessons_learned
                   SET active = 0, updated_at = datetime('now')
                   WHERE id = ?""",
                (rule["id"],),
            )
            log(f"  Pruned rule #{rule['id']} (injected {rule['times_injected']}x, "
                f"score={rule['effectiveness_score']}): {rule['lesson'][:80]}")
        pruned.append(rule)

    if not dry_run and pruned:
        conn.commit()
    conn.close()

    return pruned


# ============================================================
# MAIN ENTRY POINT (called from forgesmith.py pipeline)
# ============================================================

def run_simba(cfg, dry_run=False, role_filter=None):
    """Run the full SIMBA pipeline for targeted rule generation.

    Steps:
    1. Find high-variance episodes (same role, mixed outcomes)
    2. Find hardest cases (early_terminated, q < 0.3)
    3. For each role, call Claude to generate specific rules
    4. Validate and store rules (capped at 3 per role)
    5. Prune stale rules (inject_count > 50, no improvement)

    Returns dict with results per role.
    """
    lookback = cfg.get("lookback_days", 30)
    results = {"roles_analyzed": 0, "rules_generated": 0, "rules_pruned": 0,
               "details": {}}

    # Step 1: Find high-variance episodes
    log("Finding high-variance episodes...")
    high_variance = find_high_variance_episodes(lookback_days=lookback)

    if not high_variance:
        log("No high-variance roles found (need both successes and failures).")
        # Still check hardest cases even without variance data
        hardest = find_hardest_cases(lookback_days=lookback)
        if hardest:
            # Group hardest cases by role
            by_role = {}
            for ep in hardest:
                r = ep["role"]
                if r not in by_role:
                    by_role[r] = []
                by_role[r].append(ep)
            for r, cases in by_role.items():
                if role_filter and r != role_filter:
                    continue
                high_variance[r] = {"successes": [], "failures": cases}

    if not high_variance:
        log("No episodes to analyze.")
        return results

    # Step 2: Find hardest cases
    hardest = find_hardest_cases(lookback_days=lookback)
    hardest_by_role = {}
    for ep in hardest:
        r = ep["role"]
        if r not in hardest_by_role:
            hardest_by_role[r] = []
        hardest_by_role[r].append(ep)

    # Step 3: Generate rules for each role
    for role, groups in high_variance.items():
        if role_filter and role != role_filter:
            continue

        successes = groups["successes"]
        failures = groups["failures"]
        role_hardest = hardest_by_role.get(role, [])

        total_episodes = len(successes) + len(failures)
        if total_episodes < MIN_EPISODES_FOR_ANALYSIS:
            log(f"Skipping {role}: only {total_episodes} episodes "
                f"(need {MIN_EPISODES_FOR_ANALYSIS})")
            continue

        log(f"Analyzing {role}: {len(successes)} successes, "
            f"{len(failures)} failures, {len(role_hardest)} hardest cases")
        results["roles_analyzed"] += 1

        existing_rules = get_existing_simba_rules(role)
        prompt = build_simba_prompt(
            role, successes, failures, role_hardest, existing_rules
        )

        # Call Claude
        log(f"Calling Claude for {role} rules...")
        raw_rules = call_claude_for_rules(prompt, cfg)

        if not raw_rules:
            log(f"No rules generated for {role}")
            results["details"][role] = {"generated": 0, "stored": 0}
            continue

        log(f"Claude generated {len(raw_rules)} candidate rules for {role}")

        # Validate and store
        stored = store_rules(role, raw_rules, dry_run=dry_run)
        stored_count = sum(1 for s in stored if s.get("stored", not dry_run))
        results["rules_generated"] += stored_count
        results["details"][role] = {
            "generated": len(raw_rules),
            "stored": stored_count,
            "rules": stored,
        }

    # Step 5: Evaluate existing rules before pruning
    if not dry_run:
        log("Evaluating existing SIMBA rules...")
        evaluated = evaluate_simba_rules()
        results["rules_evaluated"] = evaluated
        if evaluated:
            log(f"  Evaluated {evaluated} rules")

    # Step 6: Prune stale rules (only those confirmed ineffective)
    log("Pruning stale rules...")
    pruned = prune_stale_rules(dry_run=dry_run)
    results["rules_pruned"] = len(pruned)

    # Summary
    log(f"SIMBA complete: {results['roles_analyzed']} roles analyzed, "
        f"{results['rules_generated']} rules generated, "
        f"{results['rules_pruned']} rules pruned")

    return results


# ============================================================
# STANDALONE CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="ForgeSmith SIMBA — Targeted rule generation from failure patterns")
    parser.add_argument("--role", metavar="ROLE",
                        help="Only analyze a specific role (default: all roles)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show proposed rules without storing")
    parser.add_argument("--prune", action="store_true",
                        help="Only run the prune step")
    parser.add_argument("--lookback", type=int, default=30,
                        help="Days of history to analyze (default: 30)")

    args = parser.parse_args()

    cfg = {"lookback_days": args.lookback}

    if args.prune:
        log("Running prune step only...")
        pruned = prune_stale_rules(dry_run=args.dry_run)
        log(f"Pruned {len(pruned)} stale rules")
        return

    results = run_simba(cfg, dry_run=args.dry_run, role_filter=args.role)

    # Print summary
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
