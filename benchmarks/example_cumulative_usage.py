"""Example usage of CumulativeDB for Docker benchmark runners.

Demonstrates the cumulative knowledge pattern:
1. Initialize master DB
2. Inject into container before task
3. Extract and merge after task
4. Repeat for next container
"""

import logging
from pathlib import Path

import docker

from benchmarks.cumulative_db import CumulativeDB

logging.basicConfig(level=logging.INFO)


def run_benchmark_with_cumulative_db(
    task_instances: list[str],
    master_db_path: str = "/tmp/master_theforge.db",
    container_db_path: str = "/app/theforge.db",
) -> None:
    """Run benchmark tasks with cumulative knowledge.

    Args:
        task_instances: List of task instance IDs to run
        master_db_path: Path to master DB on host
        container_db_path: Path to DB inside containers
    """
    client = docker.from_env()
    cumulative_db = CumulativeDB(master_db_path)

    print(f"Starting cumulative benchmark with {len(task_instances)} tasks")
    print(f"Master DB: {master_db_path}")

    for i, instance_id in enumerate(task_instances, 1):
        print(f"\n[{i}/{len(task_instances)}] Running task: {instance_id}")

        # Create container
        container = client.containers.create(
            image="equipa:latest",
            command=["python", "run_task.py", instance_id],
            detach=True,
        )

        try:
            # Inject accumulated knowledge before running
            cumulative_db.inject_into_container(container, container_db_path)

            # Run task
            container.start()
            result = container.wait()

            print(f"    Exit code: {result['StatusCode']}")

            # Extract and merge new knowledge
            cumulative_db.extract_and_merge(container, container_db_path)

            # Show stats
            stats = cumulative_db.get_stats()
            print(f"    Cumulative stats: {stats}")

        finally:
            container.remove(force=True)

    print(f"\n=== Final cumulative knowledge ===")
    final_stats = cumulative_db.get_stats()
    for key, value in final_stats.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    # Example: Run 5 tasks with cumulative learning
    task_ids = [
        "psf__requests-1",
        "django__django-100",
        "pallets__flask-50",
        "pytest-dev__pytest-200",
        "scikit-learn__scikit-learn-300",
    ]

    run_benchmark_with_cumulative_db(task_ids)
