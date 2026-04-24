"""Tests for equipa_harness_sweep.py — CLI and report generation.

Copyright 2026, Forgeborn
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

# Add project root to path for import
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from equipa_harness_sweep import (
    RunResult,
    SweepConfig,
    build_comparison_table,
    ensure_sweep_table,
    generate_report,
    parse_args,
    _parse_outcome,
    _outcome_icon,
    _extract_patch_size,
    _extract_turns_used,
    _extract_cost,
    ANCHOR_SETS,
)


# --- Fixtures ---


@pytest.fixture
def sweep_config() -> SweepConfig:
    """A minimal sweep config with fake results for table/report tests."""
    config = SweepConfig(
        sweep_id="sweep-model-20260424-abc123",
        parameter_name="model",
        parameter_values=["sonnet", "opus"],
        anchor_set="swebench",
        task_ids=["django__django-16379", "sympy__sympy-24152"],
    )
    config.results = [
        RunResult(
            task_id="django__django-16379",
            parameter_name="model",
            parameter_value="sonnet",
            outcome="resolved",
            duration_seconds=120.5,
            turns_used=8,
            cost_usd=0.42,
            patch_size=1500,
        ),
        RunResult(
            task_id="django__django-16379",
            parameter_name="model",
            parameter_value="opus",
            outcome="resolved",
            duration_seconds=95.3,
            turns_used=6,
            cost_usd=1.20,
            patch_size=1200,
        ),
        RunResult(
            task_id="sympy__sympy-24152",
            parameter_name="model",
            parameter_value="sonnet",
            outcome="failed",
            duration_seconds=200.0,
            turns_used=15,
            cost_usd=0.80,
        ),
        RunResult(
            task_id="sympy__sympy-24152",
            parameter_name="model",
            parameter_value="opus",
            outcome="resolved",
            duration_seconds=150.2,
            turns_used=10,
            cost_usd=2.50,
            patch_size=3000,
        ),
    ]
    return config


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Create a temporary SQLite DB for sweep_results tests."""
    db_path = tmp_path / "test_theforge.db"
    conn = sqlite3.connect(str(db_path))
    ensure_sweep_table(conn)
    conn.close()
    return db_path


# --- parse_args tests ---


def test_parse_args_basic():
    args = parse_args(["--param", "model", "--values", "a,b"])
    assert args.param == "model"
    assert args.values == "a,b"
    assert args.anchor == "swebench"
    assert not args.dry_run


def test_parse_args_dry_run():
    args = parse_args(["--param", "model", "--values", "a,b", "--dry-run"])
    assert args.dry_run is True


def test_parse_args_custom_tasks():
    args = parse_args([
        "--param", "model",
        "--values", "a,b",
        "--tasks", "task1,task2",
    ])
    assert args.tasks == "task1,task2"


def test_parse_args_list_sweeps():
    args = parse_args(["--list-sweeps"])
    assert args.list_sweeps is True


# --- RunResult tests ---


def test_run_result_to_dict():
    r = RunResult(
        task_id="t1",
        parameter_name="model",
        parameter_value="opus",
        outcome="resolved",
        duration_seconds=10.5,
        turns_used=3,
        cost_usd=0.5,
    )
    d = r.to_dict()
    assert d["task_id"] == "t1"
    assert d["outcome"] == "resolved"
    assert d["cost_usd"] == 0.5


# --- SweepConfig tests ---


def test_generate_sweep_id():
    sid = SweepConfig.generate_sweep_id("model")
    assert sid.startswith("sweep-model-")
    assert len(sid) > 20


# --- Outcome parsing ---


@pytest.mark.parametrize(
    "stdout,returncode,expected",
    [
        ("RESOLVED task", 0, "resolved"),
        ("RESULT: success", 0, "resolved"),
        ("RESULT: blocked", 1, "blocked"),
        ("some error", 1, "failed"),
        ("nothing useful", 0, "failed"),
    ],
)
def test_parse_outcome(stdout: str, returncode: int, expected: str):
    assert _parse_outcome(stdout, returncode) == expected


# --- Extraction helpers ---


def test_extract_patch_size():
    stdout = "RESOLVED — 2792 char patch generated"
    assert _extract_patch_size(stdout) == 2792


def test_extract_patch_size_missing():
    assert _extract_patch_size("no patch info here") == 0


def test_extract_turns_used():
    stdout = "Turns used: 12 of 45"
    assert _extract_turns_used(stdout) == 12


def test_extract_turns_used_missing():
    assert _extract_turns_used("no turn info") == 0


def test_extract_cost():
    stdout = "Total cost: $1.42"
    assert _extract_cost(stdout) == 1.42


def test_extract_cost_missing():
    assert _extract_cost("no cost here") == 0.0


# --- Comparison table ---


def test_build_comparison_table(sweep_config: SweepConfig):
    table = build_comparison_table(sweep_config)
    assert "sonnet" in table
    assert "opus" in table
    assert "django__django-16379" in table
    assert "[PASS]" in table
    assert "[FAIL]" in table
    assert "Pass rate" in table


# --- Outcome icons ---


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("resolved", "[PASS]"),
        ("failed", "[FAIL]"),
        ("timeout", "[TIME]"),
        ("error", "[ERR]"),
        ("unknown", "[?]"),
    ],
)
def test_outcome_icon(outcome: str, expected: str):
    assert _outcome_icon(outcome) == expected


# --- Report generation ---


def test_generate_report(sweep_config: SweepConfig, tmp_path: Path):
    with patch(
        "equipa_harness_sweep.SWEEPS_DIR", tmp_path / "sweeps"
    ):
        report_path = generate_report(sweep_config)

    assert report_path.exists()
    content = report_path.read_text()
    assert "Harness Sweep Report" in content
    assert "model" in content
    assert "sonnet" in content
    assert "opus" in content
    assert "Side-by-Side Comparison" in content
    assert "Aggregate Statistics" in content
    assert "Raw Results" in content
    assert "Forgeborn" in content

    # Verify raw JSON is valid
    json_start = content.index("```json\n") + 8
    json_end = content.index("\n```", json_start)
    raw = json.loads(content[json_start:json_end])
    assert len(raw) == 4


# --- DB persistence ---


def test_ensure_sweep_table(tmp_db: Path):
    conn = sqlite3.connect(str(tmp_db))
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sweep_results'"
    )
    assert cursor.fetchone() is not None
    conn.close()


def test_save_and_read_results(sweep_config: SweepConfig, tmp_db: Path):
    with patch("equipa_harness_sweep.THEFORGE_DB", str(tmp_db)):
        from equipa_harness_sweep import save_results_to_db

        count = save_results_to_db(sweep_config)

    assert count == 4

    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM sweep_results WHERE sweep_id = ?",
        (sweep_config.sweep_id,),
    ).fetchall()
    conn.close()

    assert len(rows) == 4
    outcomes = {r["outcome"] for r in rows}
    assert "resolved" in outcomes
    assert "failed" in outcomes


# --- Anchor sets ---


def test_anchor_sets_have_three_tasks():
    for name, tasks in ANCHOR_SETS.items():
        assert len(tasks) == 3, f"Anchor set '{name}' must have exactly 3 tasks"


# --- main() edge cases ---


def test_main_missing_param():
    from equipa_harness_sweep import main

    assert main(["--values", "a,b"]) == 1


def test_main_single_value():
    from equipa_harness_sweep import main

    assert main(["--param", "model", "--values", "only_one"]) == 1
