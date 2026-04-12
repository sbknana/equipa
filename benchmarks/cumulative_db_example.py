"""Example usage of CumulativeDB with Docker benchmark runners.

This demonstrates how to integrate the cumulative knowledge pattern into
a Docker-based benchmark harness like FeatureBench.
"""

from __future__ import annotations

import logging
from pathlib import Path

import docker

from benchmarks.cumulative_db import CumulativeDB

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def run_benchmark_with_cumulative_knowledge() -> None:
    """Run multiple benchmark tasks with cumulative knowledge accumulation."""

    # Initialize Docker client
    client = docker.from_env()

    # Initialize cumulative DB (master DB on host)
    master_db_path = Path("/tmp/benchmark_master.db")
    cumulative_db = CumulativeDB(str(master_db_path))

    # Example: Run 5 benchmark tasks
    tasks = [
        {"repo": "fastapi/fastapi", "task": "Add input validation"},
        {"repo": "requests/requests", "task": "Fix timeout handling"},
        {"repo": "flask/flask", "task": "Add route middleware"},
        {"repo": "django/django", "task": "Optimize query performance"},
        {"repo": "pandas/pandas", "task": "Add data transformation"},
    ]

    for i, task in enumerate(tasks, 1):
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Task {i}/{len(tasks)}: {task['task']}")
        logger.info(f"{'=' * 60}")

        # Create container for this task
        container = client.containers.create(
            image="equipa:latest",
            command=[
                "python", "-m", "equipa.dispatch",
                "--task", task["task"],
                "--repo", task["repo"]
            ],
            detach=True,
            working_dir="/app"
        )

        try:
            # STEP 1: Inject master DB into container
            # The container starts with ALL accumulated knowledge from previous tasks
            cumulative_db.inject_into_container(container, "/app/theforge.db")
            logger.info(f"✓ Injected master DB into container {container.short_id}")

            # STEP 2: Run the task
            container.start()
            result = container.wait()
            exit_code = result["StatusCode"]

            # Get logs
            logs = container.logs().decode("utf-8")
            logger.info(f"Container logs:\n{logs[-500:]}")  # Last 500 chars

            if exit_code == 0:
                logger.info(f"✓ Task completed successfully")
            else:
                logger.warning(f"✗ Task failed with exit code {exit_code}")

            # STEP 3: Extract and merge DB from container
            # This captures new lessons/episodes/decisions from this task run
            cumulative_db.extract_and_merge(container, "/app/theforge.db")
            logger.info(f"✓ Extracted and merged DB from container")

            # Show cumulative stats
            stats = cumulative_db.get_stats()
            logger.info(f"Cumulative knowledge stats: {stats}")

        finally:
            # Clean up container
            container.remove(force=True)

    # Final summary
    logger.info(f"\n{'=' * 60}")
    logger.info("BENCHMARK COMPLETE")
    logger.info(f"{'=' * 60}")

    final_stats = cumulative_db.get_stats()
    logger.info(f"Total accumulated knowledge:")
    logger.info(f"  - Lessons learned: {final_stats['lessons_merged']}")
    logger.info(f"  - Agent episodes: {final_stats['episodes_merged']}")
    logger.info(f"  - Decisions made: {final_stats['decisions_merged']}")
    logger.info(f"  - Agent runs: {final_stats['runs_merged']}")
    logger.info(f"  - Session notes: {final_stats['notes_merged']}")
    logger.info(f"\nMaster DB saved at: {master_db_path}")


def integrate_with_featurebench() -> None:
    """Show how to integrate with existing FeatureBench runner.

    This example shows the minimal changes needed to add cumulative knowledge
    to featurebench_docker.py.
    """

    # At the start of the benchmark run (before the loop):
    master_db_path = Path("/tmp/featurebench_master.db")
    cumulative_db = CumulativeDB(str(master_db_path))

    # For each task in the benchmark:
    # tasks = load_featurebench_tasks()
    # for task in tasks:

    # BEFORE running the container (after container.create):
    # cumulative_db.inject_into_container(container, "/app/theforge.db")

    # AFTER the task completes (after container.wait):
    # cumulative_db.extract_and_merge(container, "/app/theforge.db")
    # stats = cumulative_db.get_stats()
    # logger.info(f"Cumulative stats: {stats}")

    # That's it! The container now benefits from all prior task knowledge.


if __name__ == "__main__":
    # Uncomment to run the example:
    # run_benchmark_with_cumulative_knowledge()

    logger.info("See function docstrings for usage examples")
    logger.info("To integrate with FeatureBench, add 3 lines of code:")
    logger.info("  1. Initialize CumulativeDB before task loop")
    logger.info("  2. Inject DB before starting each container")
    logger.info("  3. Extract and merge DB after each task completes")
