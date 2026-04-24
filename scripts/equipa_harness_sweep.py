#!/usr/bin/env python3
"""EQUIPA Harness Sweep — One-knob-at-a-time model/prompt tuning discipline.

Codifies a rigorous parameter sweep process for evaluating EQUIPA changes.
Runs a fixed set of anchor tasks with one parameter varied at a time, then
produces a side-by-side comparison table and writes results to TheForge.

Usage:
    # Sweep across models
    python3 scripts/equipa_harness_sweep.py --param model \
        --values "claude-sonnet-4-6,claude-opus-4-6" \
        --tasks "django__django-16379,sympy__sympy-24152,astropy__astropy-14995"

    # Sweep max_turns
    python3 scripts/equipa_harness_sweep.py --param max_turns \
        --values "15,25,45" \
        --anchor swebench

    # Sweep a prompt section (reads from prompts/ dir)
    python3 scripts/equipa_harness_sweep.py --param prompt_section \
        --values "developer_v1.md,developer_v2.md"

    # Dry-run (show what would execute, no actual runs)
    python3 scripts/equipa_harness_sweep.py --param model \
        --values "claude-sonnet-4-6,claude-opus-4-6" --dry-run

    # List previous sweeps
    python3 scripts/equipa_harness_sweep.py --list-sweeps

Copyright 2026, Forgeborn
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# --- Constants ---

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
BENCHMARKS_DIR = PROJECT_ROOT / "benchmarks"
SWEEPS_DIR = BENCHMARKS_DIR / "sweeps"
PROMPTS_DIR = SCRIPT_DIR / "prompts"

# Fixed anchor task sets for reproducible evaluation
ANCHOR_SETS: dict[str, list[str]] = {
    "swebench": [
        "django__django-16379",
        "sympy__sympy-24152",
        "astropy__astropy-14995",
    ],
    "featurebench": [
        "fb-todo-api",
        "fb-csv-parser",
        "fb-auth-middleware",
    ],
}

DEFAULT_ANCHOR = "swebench"

# TheForge DB resolution (matches EQUIPA convention)
THEFORGE_DB = os.environ.get(
    "THEFORGE_DB",
    str(Path("/srv/forge-share/AI_Stuff/TheForge/theforge.db")),
)
EQUIPA_PROJECT_ID = 23


@dataclass
class RunResult:
    """Result of a single task run within a sweep."""

    task_id: str
    parameter_name: str
    parameter_value: str
    outcome: str  # "resolved", "failed", "error", "timeout"
    duration_seconds: float = 0.0
    turns_used: int = 0
    cost_usd: float = 0.0
    error_message: str = ""
    patch_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "parameter_name": self.parameter_name,
            "parameter_value": self.parameter_value,
            "outcome": self.outcome,
            "duration_seconds": self.duration_seconds,
            "turns_used": self.turns_used,
            "cost_usd": self.cost_usd,
            "error_message": self.error_message,
            "patch_size": self.patch_size,
        }


@dataclass
class SweepConfig:
    """Configuration for a single sweep run."""

    sweep_id: str
    parameter_name: str
    parameter_values: list[str]
    anchor_set: str
    task_ids: list[str]
    dry_run: bool = False
    timeout_seconds: int = 600
    results: list[RunResult] = field(default_factory=list)

    @staticmethod
    def generate_sweep_id(param_name: str) -> str:
        """Generate a unique sweep ID from parameter name and timestamp."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        short_hash = hashlib.sha256(
            f"{param_name}-{ts}".encode()
        ).hexdigest()[:6]
        return f"sweep-{param_name}-{ts}-{short_hash}"


def get_db_connection() -> sqlite3.Connection:
    """Get a connection to TheForge database.

    Returns:
        sqlite3.Connection with row_factory set.

    Raises:
        SystemExit: If database file does not exist.
    """
    db_path = Path(THEFORGE_DB)
    if not db_path.exists():
        print(f"ERROR: TheForge DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_sweep_table(conn: sqlite3.Connection) -> None:
    """Create the sweep_results table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sweep_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sweep_id TEXT NOT NULL,
            parameter_name TEXT NOT NULL,
            parameter_value TEXT NOT NULL,
            task_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            duration_seconds REAL DEFAULT 0.0,
            turns_used INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            error_message TEXT DEFAULT '',
            patch_size INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(sweep_id, parameter_value, task_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sweep_results_sweep_id
        ON sweep_results(sweep_id)
    """)
    conn.commit()


def run_single_task(
    task_id: str,
    param_name: str,
    param_value: str,
    timeout_seconds: int,
) -> RunResult:
    """Execute a single benchmark task via Claude CLI with Max auth.

    Uses subprocess to invoke the EQUIPA runner or Claude CLI directly.
    Never uses API keys — always Max subscription auth.

    Args:
        task_id: The benchmark task identifier.
        param_name: Which parameter is being swept.
        param_value: The value of the swept parameter for this run.
        timeout_seconds: Max seconds before killing the run.

    Returns:
        RunResult with outcome and metrics.
    """
    start_time = time.monotonic()

    # Build the CLI command based on parameter type
    cmd = _build_run_command(task_id, param_name, param_value)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(PROJECT_ROOT),
        )
        elapsed = time.monotonic() - start_time

        outcome = _parse_outcome(result.stdout, result.returncode)
        patch_size = _extract_patch_size(result.stdout)
        turns_used = _extract_turns_used(result.stdout)
        cost = _extract_cost(result.stdout)

        return RunResult(
            task_id=task_id,
            parameter_name=param_name,
            parameter_value=param_value,
            outcome=outcome,
            duration_seconds=round(elapsed, 2),
            turns_used=turns_used,
            cost_usd=cost,
            patch_size=patch_size,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start_time
        return RunResult(
            task_id=task_id,
            parameter_name=param_name,
            parameter_value=param_value,
            outcome="timeout",
            duration_seconds=round(elapsed, 2),
            error_message=f"Timed out after {timeout_seconds}s",
        )
    except OSError as exc:
        elapsed = time.monotonic() - start_time
        return RunResult(
            task_id=task_id,
            parameter_name=param_name,
            parameter_value=param_value,
            outcome="error",
            duration_seconds=round(elapsed, 2),
            error_message=str(exc),
        )


def _build_run_command(
    task_id: str, param_name: str, param_value: str
) -> list[str]:
    """Build the subprocess command list for a benchmark run.

    Always uses Max auth (claude CLI with --model flag, no --api-key).

    Args:
        task_id: Benchmark task ID.
        param_name: The parameter being swept.
        param_value: The current value for this run.

    Returns:
        Command as a list of strings for subprocess.
    """
    base_cmd = [
        "python3",
        str(PROJECT_ROOT / "benchmarks" / "swebench_docker.py"),
    ]

    if param_name == "model":
        return [*base_cmd, "--task", task_id, "--model", param_value]
    elif param_name == "max_turns":
        return [
            *base_cmd,
            "--task",
            task_id,
            "--max-turns",
            param_value,
        ]
    elif param_name == "prompt_section":
        return [
            *base_cmd,
            "--task",
            task_id,
            "--prompt-override",
            str(PROMPTS_DIR / param_value),
        ]
    else:
        return [
            *base_cmd,
            "--task",
            task_id,
            f"--{param_name.replace('_', '-')}",
            param_value,
        ]


def _parse_outcome(stdout: str, returncode: int) -> str:
    """Parse task outcome from runner stdout."""
    stdout_lower = stdout.lower()
    if returncode == 0 and "resolved" in stdout_lower:
        return "resolved"
    if "result: success" in stdout_lower:
        return "resolved"
    if "result: blocked" in stdout_lower:
        return "blocked"
    if returncode != 0:
        return "failed"
    return "failed"


def _extract_patch_size(stdout: str) -> int:
    """Extract patch size in characters from runner output."""
    for line in stdout.splitlines():
        if "patch" in line.lower() and "char" in line.lower():
            parts = line.split()
            for i, part in enumerate(parts):
                if part.isdigit() and i + 1 < len(parts):
                    if "char" in parts[i + 1].lower():
                        return int(part)
    return 0


def _extract_turns_used(stdout: str) -> int:
    """Extract number of turns used from runner output."""
    for line in stdout.splitlines():
        lower = line.lower()
        if "turns" in lower and ("used" in lower or "total" in lower):
            for word in line.split():
                if word.isdigit():
                    return int(word)
    return 0


def _extract_cost(stdout: str) -> float:
    """Extract cost in USD from runner output."""
    for line in stdout.splitlines():
        if "$" in line and ("cost" in line.lower() or "total" in line.lower()):
            for word in line.replace("$", "").split():
                try:
                    val = float(word)
                    if 0 < val < 1000:
                        return round(val, 4)
                except ValueError:
                    continue
    return 0.0


def execute_sweep(config: SweepConfig) -> None:
    """Execute a full parameter sweep across all values and tasks.

    For each parameter value, runs all anchor tasks and collects results.
    Results are stored in config.results.

    Args:
        config: The sweep configuration.
    """
    total_runs = len(config.parameter_values) * len(config.task_ids)
    completed = 0

    print(f"\n{'='*60}")
    print(f"  EQUIPA Harness Sweep: {config.sweep_id}")
    print(f"  Parameter: {config.parameter_name}")
    print(f"  Values: {', '.join(config.parameter_values)}")
    print(f"  Anchor set: {config.anchor_set}")
    print(f"  Tasks: {len(config.task_ids)}")
    print(f"  Total runs: {total_runs}")
    print(f"{'='*60}\n")

    for value in config.parameter_values:
        print(f"\n--- {config.parameter_name}={value} ---")

        for task_id in config.task_ids:
            completed += 1
            print(
                f"  [{completed}/{total_runs}] {task_id} ... ",
                end="",
                flush=True,
            )

            if config.dry_run:
                cmd = _build_run_command(
                    task_id, config.parameter_name, value
                )
                print(f"DRY-RUN: {' '.join(cmd)}")
                config.results.append(
                    RunResult(
                        task_id=task_id,
                        parameter_name=config.parameter_name,
                        parameter_value=value,
                        outcome="dry-run",
                    )
                )
                continue

            result = run_single_task(
                task_id,
                config.parameter_name,
                value,
                config.timeout_seconds,
            )
            config.results.append(result)
            print(
                f"{result.outcome} "
                f"({result.duration_seconds:.1f}s, "
                f"{result.turns_used} turns, "
                f"${result.cost_usd:.2f})"
            )


def save_results_to_db(config: SweepConfig) -> int:
    """Persist sweep results to TheForge database.

    Creates the sweep_results table if needed, then batch-inserts
    all results for this sweep.

    Args:
        config: Completed sweep config with results.

    Returns:
        Number of rows inserted.
    """
    conn = get_db_connection()
    try:
        ensure_sweep_table(conn)
        rows_inserted = 0

        for result in config.results:
            conn.execute(
                """
                INSERT OR REPLACE INTO sweep_results
                    (sweep_id, parameter_name, parameter_value,
                     task_id, outcome, duration_seconds,
                     turns_used, cost_usd, error_message, patch_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    config.sweep_id,
                    result.parameter_name,
                    result.parameter_value,
                    result.task_id,
                    result.outcome,
                    result.duration_seconds,
                    result.turns_used,
                    result.cost_usd,
                    result.error_message,
                    result.patch_size,
                ),
            )
            rows_inserted += 1

        conn.commit()
        print(f"\nSaved {rows_inserted} results to TheForge DB.")
        return rows_inserted
    finally:
        conn.close()


def build_comparison_table(config: SweepConfig) -> str:
    """Build a side-by-side markdown comparison table from sweep results.

    Rows = tasks, columns = parameter values. Each cell shows
    outcome, duration, and turns.

    Args:
        config: Completed sweep config with results.

    Returns:
        Markdown-formatted comparison table string.
    """
    values = config.parameter_values
    tasks = config.task_ids

    # Header row
    header = f"| Task | {' | '.join(values)} |"
    separator = f"|{'---|' * (len(values) + 1)}"

    rows = []
    for task_id in tasks:
        cells = [f"**{task_id}**"]
        for value in values:
            matching = [
                r
                for r in config.results
                if r.task_id == task_id and r.parameter_value == value
            ]
            if matching:
                r = matching[0]
                icon = _outcome_icon(r.outcome)
                cell = (
                    f"{icon} {r.outcome}<br>"
                    f"{r.duration_seconds:.0f}s / "
                    f"{r.turns_used}t / "
                    f"${r.cost_usd:.2f}"
                )
            else:
                cell = "—"
            cells.append(cell)
        rows.append(f"| {' | '.join(cells)} |")

    # Summary row
    summary_cells = ["**Pass rate**"]
    for value in values:
        value_results = [
            r for r in config.results if r.parameter_value == value
        ]
        resolved = sum(1 for r in value_results if r.outcome == "resolved")
        total = len(value_results) if value_results else 1
        pct = (resolved / total) * 100
        total_cost = sum(r.cost_usd for r in value_results)
        summary_cells.append(f"**{resolved}/{total} ({pct:.0f}%)**<br>${total_cost:.2f} total")
    rows.append(f"| {' | '.join(summary_cells)} |")

    return "\n".join([header, separator, *rows])


def _outcome_icon(outcome: str) -> str:
    """Map outcome to a text marker for the comparison table."""
    return {
        "resolved": "[PASS]",
        "failed": "[FAIL]",
        "timeout": "[TIME]",
        "error": "[ERR]",
        "blocked": "[BLOK]",
        "dry-run": "[DRY]",
    }.get(outcome, "[?]")


def generate_report(config: SweepConfig) -> Path:
    """Generate a markdown report for the sweep and write to disk.

    Report is saved to benchmarks/sweeps/{sweep_id}.md.

    Args:
        config: Completed sweep config with results.

    Returns:
        Path to the generated report file.
    """
    SWEEPS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = SWEEPS_DIR / f"{config.sweep_id}.md"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    comparison = build_comparison_table(config)

    # Compute aggregate stats per value
    agg_lines = []
    for value in config.parameter_values:
        value_results = [
            r for r in config.results if r.parameter_value == value
        ]
        resolved = sum(1 for r in value_results if r.outcome == "resolved")
        total = len(value_results)
        avg_time = (
            sum(r.duration_seconds for r in value_results) / total
            if total
            else 0
        )
        avg_turns = (
            sum(r.turns_used for r in value_results) / total if total else 0
        )
        total_cost = sum(r.cost_usd for r in value_results)
        agg_lines.append(
            f"| `{value}` | {resolved}/{total} "
            f"({(resolved/total*100) if total else 0:.0f}%) "
            f"| {avg_time:.1f}s | {avg_turns:.1f} | ${total_cost:.2f} |"
        )

    agg_table = "\n".join(
        [
            "| Value | Pass Rate | Avg Time | Avg Turns | Total Cost |",
            "|-------|-----------|----------|-----------|------------|",
            *agg_lines,
        ]
    )

    # Build raw results JSON for reproducibility
    raw_json = json.dumps(
        [r.to_dict() for r in config.results], indent=2
    )

    report = f"""# Harness Sweep Report: {config.sweep_id}

**Generated:** {now}
**Parameter:** `{config.parameter_name}`
**Values:** {', '.join(f'`{v}`' for v in config.parameter_values)}
**Anchor set:** {config.anchor_set}
**Tasks:** {', '.join(config.task_ids)}

## Methodology

One-knob-at-a-time sweep. All runs use the same fixed anchor tasks to ensure
comparable results. Only `{config.parameter_name}` varies between runs.
Authentication: Max subscription only (no API keys).

## Side-by-Side Comparison

{comparison}

## Aggregate Statistics

{agg_table}

## Recommendation

<!-- Fill in after reviewing results -->
_Based on the data above, the recommended value for `{config.parameter_name}` is: **TBD**_

## Raw Results

<details>
<summary>Click to expand raw JSON</summary>

```json
{raw_json}
```

</details>

---
*Generated by equipa_harness_sweep.py — Copyright 2026, Forgeborn*
"""

    report_path.write_text(report, encoding="utf-8")
    print(f"\nReport written to: {report_path}")
    return report_path


def list_previous_sweeps() -> None:
    """List all previous sweep runs from TheForge DB."""
    conn = get_db_connection()
    try:
        ensure_sweep_table(conn)
        rows = conn.execute("""
            SELECT
                sweep_id,
                parameter_name,
                COUNT(DISTINCT parameter_value) AS num_values,
                COUNT(*) AS total_runs,
                SUM(CASE WHEN outcome = 'resolved' THEN 1 ELSE 0 END) AS resolved,
                ROUND(SUM(cost_usd), 2) AS total_cost,
                MIN(created_at) AS started
            FROM sweep_results
            GROUP BY sweep_id
            ORDER BY started DESC
            LIMIT 20
        """).fetchall()

        if not rows:
            print("No previous sweeps found.")
            return

        print(f"\n{'Sweep ID':<50} {'Param':<15} {'Vals':>4} "
              f"{'Runs':>4} {'Pass':>4} {'Cost':>8} {'Date'}")
        print("-" * 110)
        for row in rows:
            total = row["total_runs"] or 1
            pct = f"{row['resolved']}/{total}"
            print(
                f"{row['sweep_id']:<50} "
                f"{row['parameter_name']:<15} "
                f"{row['num_values']:>4} "
                f"{total:>4} "
                f"{pct:>4} "
                f"${row['total_cost']:>7.2f} "
                f"{row['started']}"
            )
    finally:
        conn.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Optional argument list (defaults to sys.argv[1:]).

    Returns:
        Parsed namespace.
    """
    parser = argparse.ArgumentParser(
        description="EQUIPA Harness Sweep — one-knob-at-a-time tuning",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Sweep models
  %(prog)s --param model --values "claude-sonnet-4-6,claude-opus-4-6"

  # Sweep max_turns
  %(prog)s --param max_turns --values "15,25,45"

  # Sweep prompt variants
  %(prog)s --param prompt_section --values "developer_v1.md,developer_v2.md"

  # Use FeatureBench anchor tasks instead of SWE-bench
  %(prog)s --param model --values "sonnet,opus" --anchor featurebench

  # Custom task set
  %(prog)s --param model --values "sonnet,opus" \\
      --tasks "django__django-16379,sympy__sympy-24152"

  # Dry run
  %(prog)s --param model --values "sonnet,opus" --dry-run

  # List previous sweeps
  %(prog)s --list-sweeps
        """,
    )

    parser.add_argument(
        "--param",
        type=str,
        help="Parameter to sweep (model, max_turns, prompt_section, etc.)",
    )
    parser.add_argument(
        "--values",
        type=str,
        help="Comma-separated list of values to test",
    )
    parser.add_argument(
        "--anchor",
        type=str,
        default=DEFAULT_ANCHOR,
        choices=list(ANCHOR_SETS.keys()),
        help=f"Pre-defined anchor task set (default: {DEFAULT_ANCHOR})",
    )
    parser.add_argument(
        "--tasks",
        type=str,
        help="Override: comma-separated task IDs (replaces anchor set)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Timeout per task in seconds (default: 600)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing",
    )
    parser.add_argument(
        "--list-sweeps",
        action="store_true",
        help="List previous sweep runs and exit",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the harness sweep CLI.

    Args:
        argv: Optional argument list for testing.

    Returns:
        Exit code (0 = success, 1 = error).
    """
    args = parse_args(argv)

    if args.list_sweeps:
        list_previous_sweeps()
        return 0

    if not args.param or not args.values:
        print(
            "ERROR: --param and --values are required "
            "(use --list-sweeps to see history)",
            file=sys.stderr,
        )
        return 1

    values = [v.strip() for v in args.values.split(",") if v.strip()]
    if len(values) < 2:
        print(
            "ERROR: need at least 2 values to compare "
            "(got: {})".format(len(values)),
            file=sys.stderr,
        )
        return 1

    if args.tasks:
        task_ids = [t.strip() for t in args.tasks.split(",") if t.strip()]
        anchor_name = "custom"
    else:
        task_ids = ANCHOR_SETS[args.anchor]
        anchor_name = args.anchor

    sweep_id = SweepConfig.generate_sweep_id(args.param)
    config = SweepConfig(
        sweep_id=sweep_id,
        parameter_name=args.param,
        parameter_values=values,
        anchor_set=anchor_name,
        task_ids=task_ids,
        dry_run=args.dry_run,
        timeout_seconds=args.timeout,
    )

    execute_sweep(config)

    if not config.dry_run:
        save_results_to_db(config)

    generate_report(config)

    print(f"\nSweep complete: {sweep_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
