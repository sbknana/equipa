# Cumulative DB Usage Guide

## Overview

The `CumulativeDB` class implements the extract/merge pattern for Docker-based benchmark runners. It enables cumulative learning across multiple container runs by:

1. Extracting the TheForge DB from each container after task completion
2. Merging lessons, decisions, and episodes into a persistent master DB on the host
3. Injecting the updated master DB into the next container

This creates a feedback loop where each agent run benefits from the accumulated knowledge of all prior runs.

## Quick Start

```python
from pathlib import Path
import docker
from cumulative_db import CumulativeDB

# Initialize with master DB path
master_db = Path("/path/to/benchmark_results/master_theforge.db")
cumulative_db = CumulativeDB(str(master_db))

# Connect to Docker
client = docker.from_env()

# For each benchmark task:
for task in tasks:
    # 1. Create container
    container = client.containers.create(
        image="equipa-benchmark:latest",
        # ... other config
    )

    # 2. Inject master DB before starting
    cumulative_db.inject_into_container(
        container,
        dest_path="/app/theforge.db"
    )

    # 3. Start container and run task
    container.start()
    result = container.wait()

    # 4. Extract and merge DB after completion
    cumulative_db.extract_and_merge(
        container,
        source_path="/app/theforge.db"
    )

    # 5. Log merge statistics
    stats = cumulative_db.get_stats()
    print(f"Cumulative knowledge: {stats}")

    # 6. Clean up
    container.remove()
```

## Integration with FeatureBench Docker Runner

Example modification to `featurebench_docker.py`:

```python
# At script initialization
master_db_path = output_dir / "cumulative_master.db"
cumulative_db = CumulativeDB(str(master_db_path))

# In run_task_in_container(), after container creation:
try:
    # Inject master DB before starting
    cumulative_db.inject_into_container(
        container,
        dest_path=f"{EQUIPA_DOCKER_DIR}/theforge.db"
    )

    container.start()
    # ... existing task execution code ...

finally:
    # Extract and merge BEFORE destroying container
    cumulative_db.extract_and_merge(
        container,
        source_path=f"{EQUIPA_DOCKER_DIR}/theforge.db"
    )

    # Log cumulative stats
    stats = cumulative_db.get_stats()
    print(f"Cumulative knowledge after task {iid}:")
    print(f"  Lessons: {stats['lessons_merged']}")
    print(f"  Episodes: {stats['episodes_merged']}")
    print(f"  Decisions: {stats['decisions_merged']}")

    # ... existing cleanup code ...
```

## Deduplication Strategy

### Lessons (`lessons_learned`)
- **Deduplicated by content hash (SHA-256)**
- Prevents duplicate lessons from being stored multiple times
- If the same lesson is learned in multiple runs, only stored once

### Other Tables
- **No deduplication** — each run is unique
- `agent_episodes`: Full execution traces (unique per run)
- `decisions`: Architectural choices (tracked across all runs)
- `agent_runs`: Telemetry (one entry per run)
- `session_notes`: Summaries (one per session)

## Statistics

Call `get_stats()` to retrieve merge counts:

```python
stats = cumulative_db.get_stats()
# {
#   "lessons_merged": 42,
#   "episodes_merged": 10,
#   "decisions_merged": 8,
#   "runs_merged": 10,
#   "notes_merged": 5
# }
```

Statistics are cumulative across all merge operations for the lifetime of the `CumulativeDB` instance.

## Master DB Initialization

The master DB is created automatically on first use:

- If `master_db_path` doesn't exist, it's created with schema from `../schema.sql`
- If schema file doesn't exist, an empty DB is created (tables created on first merge)
- Parent directories are created automatically

## Error Handling

All methods log errors but don't raise exceptions to avoid breaking benchmark runs:

- **Extraction failure**: Logs error, skips merge for that container
- **Merge failure**: Rolls back transaction, logs error
- **Injection failure**: Logs error, container starts with empty/outdated DB

This ensures a single container failure doesn't break the entire benchmark run.

## Performance Considerations

- DB extraction uses Docker's tar archive API (efficient streaming)
- Merge operations use transactions (atomic, rollback on failure)
- Content hashing for lessons is SHA-256 (fast, collision-resistant)
- Master DB remains on host filesystem (no network overhead)

## Thread Safety

`CumulativeDB` is **not thread-safe**. If running containers in parallel:

```python
# Option 1: Use one CumulativeDB instance per thread with locks
import threading

cumulative_db = CumulativeDB(str(master_db))
db_lock = threading.Lock()

def run_task(task):
    # ... create container, inject DB ...
    try:
        # ... run task ...
    finally:
        with db_lock:
            cumulative_db.extract_and_merge(container, db_path)

# Option 2: Use separate master DBs per thread, merge at the end
```

## Limitations

- Requires Docker API access (`docker.from_env()`)
- Container DB must be at a known path (configurable via `source_path`)
- Master DB grows unbounded (no automatic cleanup of old data)
- ID columns are recreated on merge (original IDs lost)

## See Also

- `featurebench_docker.py`: Reference implementation of Docker benchmark runner
- `schema.sql`: TheForge database schema
- `test_cumulative_db.py`: Comprehensive test suite
