#!/usr/bin/env python3
"""
FeatureBench Runner for EQUIPA — Full Pipeline with Autoresearch Retry
(c) 2026 Forgeborn

Runs FeatureBench tasks through the EQUIPA orchestrator with up to N
retry attempts per task. If EQUIPA fails on a task, it retries with
fresh context. This tests whether persistence + iteration can crack
tasks that a single attempt cannot.

Claude Opus 4.5 raw gets 11% on FeatureBench. Can EQUIPA do better?

Usage:
    python featurebench_runner.py --limit 10 --retries 50 --output results.json
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EQUIPA_ROOT = Path("/srv/forge-share/AI_Stuff/Equipa")
BENCHMARKS_DIR = EQUIPA_ROOT / "benchmarks"
DATASET_PATH = BENCHMARKS_DIR / "featurebench_fast.jsonl"
THEFORGE_DB = EQUIPA_ROOT / "theforge.db"
BENCH_PROJECT = "FeatureBench-eval"


def load_dataset(path, limit=0, offset=0):
    items = []
    skipped = 0
    with open(path) as f:
        for line in f:
            if skipped < offset:
                skipped += 1
                continue
            items.append(json.loads(line))
            if limit and len(items) >= limit:
                break
    return items


def get_or_create_project():
    conn = sqlite3.connect(str(THEFORGE_DB))
    row = conn.execute(
        "SELECT id FROM projects WHERE codename = ?", (BENCH_PROJECT,)
    ).fetchone()
    if row:
        conn.close()
        return row[0]
    conn.execute(
        "INSERT INTO projects (codename, name, status, summary) VALUES (?, ?, 'active', ?)",
        (BENCH_PROJECT, "FeatureBench Evaluation",
         "Benchmark: complex feature development. Claude Opus 4.5 gets 11%."),
    )
    conn.commit()
    pid = conn.execute(
        "SELECT id FROM projects WHERE codename = ?", (BENCH_PROJECT,)
    ).fetchone()[0]
    conn.close()
    return pid


def setup_repo(instance, work_dir):
    repo = instance["repo"]
    base_commit = instance["base_commit"]
    repo_dir = work_dir / "repo"

    print(f"    [Setup] Cloning {repo} at {base_commit[:8]}...")
    try:
        subprocess.run(["git", "init", str(repo_dir)],
                       capture_output=True, check=True, timeout=10)
        subprocess.run(["git", "remote", "add", "origin",
                        f"https://github.com/{repo}.git"],
                       cwd=str(repo_dir), capture_output=True, check=True, timeout=10)
        subprocess.run(["git", "fetch", "--depth", "1", "origin", base_commit],
                       cwd=str(repo_dir), capture_output=True, check=True, timeout=180)
        subprocess.run(["git", "checkout", "FETCH_HEAD"],
                       cwd=str(repo_dir), capture_output=True, check=True, timeout=30)
        subprocess.run(["git", "checkout", "-b", "main"],
                       cwd=str(repo_dir), capture_output=True, timeout=10)
        return repo_dir
    except Exception as e:
        print(f"    [Setup] FAILED: {e}")
        return None


def update_project_path(project_id, repo_dir):
    conn = sqlite3.connect(str(THEFORGE_DB))
    conn.execute("UPDATE projects SET local_path = ? WHERE id = ?",
                 (str(repo_dir), project_id))
    conn.commit()
    conn.close()


def create_task(project_id, instance, repo_dir, attempt=1):
    iid = instance["instance_id"]
    problem = instance["problem_statement"]
    desc = (
        f"FeatureBench task: {iid} (attempt {attempt})\n\n"
        f"Implement this feature in the repository at {repo_dir}.\n\n"
        f"FEATURE REQUEST:\n{problem}\n\n"
        f"Instructions: Read the feature request carefully. Understand what "
        f"needs to be built. Implement the feature with clean, working code. "
        f"Add tests for the new functionality. Make sure existing tests still pass. "
        f"Commit your changes when done."
    )
    conn = sqlite3.connect(str(THEFORGE_DB))
    conn.execute(
        "INSERT INTO tasks (project_id, title, description, status, priority) "
        "VALUES (?, ?, ?, 'todo', 'high')",
        (project_id, f"FB: {iid} (attempt {attempt})", desc),
    )
    conn.commit()
    task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return task_id


def run_equipa(task_id, repo_dir, project_id, base_commit="", timeout=900):
    update_project_path(project_id, str(repo_dir))

    start = time.time()
    try:
        result = subprocess.run(
            ["python3", "-u", str(EQUIPA_ROOT / "forge_orchestrator.py"),
             "--task", str(task_id), "--dev-test", "-y"],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "THEFORGE_DB": str(THEFORGE_DB)},
        )
        duration = time.time() - start

        # Check for patch
        patch = ""
        for cmd in [
            ["git", "diff", "HEAD"],
            ["git", "diff", f"{base_commit}..HEAD"],
        ]:
            try:
                diff = subprocess.run(cmd, cwd=str(repo_dir),
                                      capture_output=True, text=True, timeout=10)
                if diff.stdout.strip():
                    patch = diff.stdout
                    break
            except Exception:
                pass

        # Check branches
        if not patch:
            try:
                branches = subprocess.run(
                    ["git", "branch", "--all"], cwd=str(repo_dir),
                    capture_output=True, text=True, timeout=10)
                for line in branches.stdout.strip().split("\n"):
                    branch = line.strip().lstrip("* ")
                    if "forge-task" in branch:
                        diff = subprocess.run(
                            ["git", "diff", f"FETCH_HEAD..{branch}"],
                            cwd=str(repo_dir), capture_output=True,
                            text=True, timeout=10)
                        if diff.stdout.strip():
                            patch = diff.stdout
                            break
            except Exception:
                pass

        # Check all commits
        if not patch:
            try:
                log = subprocess.run(
                    ["git", "log", "--oneline", "--all"],
                    cwd=str(repo_dir), capture_output=True, text=True, timeout=10)
                commits = log.stdout.strip().split("\n")
                if len(commits) > 1:
                    diff = subprocess.run(
                        ["git", "diff", f"{commits[-1].split()[0]}..{commits[0].split()[0]}"],
                        cwd=str(repo_dir), capture_output=True, text=True, timeout=10)
                    if diff.stdout.strip():
                        patch = diff.stdout
            except Exception:
                pass

        has_patch = bool(patch and patch.strip())
        lines = patch.split("\n") if patch else []
        changes = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
        changes += sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))

        return {
            "has_patch": has_patch and changes > 0,
            "patch": patch,
            "patch_size": len(patch),
            "changes": changes,
            "duration": duration,
            "output_tail": result.stdout[-1000:] if result.stdout else "",
        }

    except subprocess.TimeoutExpired:
        return {
            "has_patch": False, "patch": "", "patch_size": 0, "changes": 0,
            "duration": time.time() - start, "output_tail": "timeout",
        }
    except Exception as e:
        return {
            "has_patch": False, "patch": "", "patch_size": 0, "changes": 0,
            "duration": time.time() - start, "output_tail": str(e),
        }


def reset_repo(repo_dir, base_commit):
    """Reset repo to base state for a retry."""
    try:
        subprocess.run(["git", "checkout", "-f", "main"],
                       cwd=str(repo_dir), capture_output=True, timeout=10)
        subprocess.run(["git", "clean", "-fd"],
                       cwd=str(repo_dir), capture_output=True, timeout=10)
        subprocess.run(["git", "reset", "--hard", "FETCH_HEAD"],
                       cwd=str(repo_dir), capture_output=True, timeout=10)
        # Remove any forge-task branches
        branches = subprocess.run(
            ["git", "branch"], cwd=str(repo_dir),
            capture_output=True, text=True, timeout=10)
        for line in branches.stdout.strip().split("\n"):
            branch = line.strip().lstrip("* ")
            if "forge-task" in branch:
                subprocess.run(["git", "branch", "-D", branch],
                               cwd=str(repo_dir), capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def run_benchmark(limit=10, offset=0, max_retries=50, timeout=900, output_path="featurebench_results.json"):
    print(f"\n{'=' * 60}")
    print(f"  FeatureBench — EQUIPA with Autoresearch Retry")
    print(f"  Tasks: {limit} (offset {offset})")
    print(f"  Max retries per task: {max_retries}")
    print(f"  Timeout per attempt: {timeout}s")
    print(f"  Model: Opus (dev) + Sonnet (tester)")
    print(f"{'=' * 60}\n")

    dataset = load_dataset(str(DATASET_PATH), limit=limit, offset=offset)
    print(f"Loaded {len(dataset)} instances")

    project_id = get_or_create_project()
    results = []
    resolved = 0
    total_start = time.time()

    for i, instance in enumerate(dataset):
        iid = instance["instance_id"]
        repo = instance["repo"]
        print(f"\n{'━' * 60}")
        print(f"[{i+1}/{len(dataset)}] {iid}")
        print(f"  Repo: {repo}")

        work_dir = Path(tempfile.mkdtemp(prefix="fb_"))
        task_resolved = False
        attempts = 0
        total_task_time = 0

        try:
            repo_dir = setup_repo(instance, work_dir)
            if not repo_dir:
                results.append({
                    "instance_id": iid, "repo": repo, "resolved": False,
                    "attempts": 0, "reason": "setup_failed",
                    "total_duration": 0,
                })
                continue

            for attempt in range(1, max_retries + 1):
                attempts = attempt
                print(f"  [Attempt {attempt}/{max_retries}]", end=" ", flush=True)

                task_id = create_task(project_id, instance, repo_dir, attempt)
                run_result = run_equipa(task_id, repo_dir, project_id,
                                       base_commit=instance["base_commit"],
                                       timeout=timeout)
                total_task_time += run_result["duration"]

                if run_result["has_patch"]:
                    task_resolved = True
                    resolved += 1
                    print(f"✓ RESOLVED — {run_result['patch_size']} chars, "
                          f"{run_result['changes']} changes, {run_result['duration']:.0f}s")
                    break
                else:
                    print(f"✗ no patch ({run_result['duration']:.0f}s)")
                    # Reset repo for next attempt
                    reset_repo(repo_dir, instance["base_commit"])

            status = "RESOLVED" if task_resolved else f"FAILED after {attempts} attempts"
            rate = resolved / (i + 1) * 100
            print(f"  [{status}] Attempts: {attempts} | Time: {total_task_time:.0f}s | "
                  f"Running rate: {resolved}/{i+1} ({rate:.1f}%)")

            results.append({
                "instance_id": iid,
                "repo": repo,
                "resolved": task_resolved,
                "attempts": attempts,
                "total_duration": total_task_time,
                "patch": run_result.get("patch", "") if task_resolved else "",
                "reason": "resolved" if task_resolved else "exhausted_retries",
            })

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    total_time = time.time() - total_start
    rate = resolved / len(dataset) * 100 if dataset else 0

    print(f"\n{'=' * 60}")
    print(f"  FeatureBench — EQUIPA Results")
    print(f"{'=' * 60}")
    print(f"  Resolved: {resolved}/{len(dataset)} ({rate:.1f}%)")
    print(f"  Total time: {total_time/60:.1f} min")
    print(f"{'=' * 60}")
    print(f"\n  Comparison:")
    print(f"    EQUIPA (autoresearch):      {rate:.1f}%")
    print(f"    Claude Opus 4.5 raw:        11.0%")
    print(f"    Best agent on FeatureBench: ~15%  (estimated)")

    output = {
        "benchmark": "FeatureBench (fast split)",
        "system": "EQUIPA (full pipeline + autoresearch retry)",
        "model": "Opus (developer) + Sonnet (tester)",
        "max_retries": max_retries,
        "timeout_per_attempt": timeout,
        "resolved": resolved,
        "total": len(dataset),
        "resolution_rate": rate,
        "total_time_seconds": total_time,
        "results": results,
    }
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved to: {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FeatureBench EQUIPA Runner")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--retries", type=int, default=50)
    parser.add_argument("--timeout", type=int, default=900, help="Timeout per attempt (seconds)")
    parser.add_argument("--output", default="featurebench_equipa_results.json")
    args = parser.parse_args()

    run_benchmark(
        limit=args.limit, offset=args.offset,
        max_retries=args.retries, timeout=args.timeout,
        output_path=args.output,
    )
