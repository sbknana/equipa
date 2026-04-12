# Cumulative Knowledge Database for Docker Benchmarks

## Overview

The `cumulative_db.py` module implements a **cumulative knowledge pattern** for Docker-based benchmark runners. After each benchmark task completes, it extracts the TheForge database from the container, merges new lessons/decisions/episodes into a master database on the host, then injects the updated master DB into the next container.

This enables **cumulative learning** across benchmark runs — each task benefits from knowledge accumulated in all prior tasks.

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│  BENCHMARK ORCHESTRATOR (Host)                              │
│                                                              │
│  ┌──────────────┐                                           │
│  │  Master DB   │  ← Accumulates all knowledge              │
│  └──────────────┘                                           │
│         │                                                    │
│         │ inject                                             │
│         ▼                                                    │
│  ┌──────────────────────────────────────┐                   │
│  │  Docker Container (Task 1)           │                   │
│  │  ┌──────────────┐                    │                   │
│  │  │  theforge.db │ ← Starts empty     │                   │
│  │  └──────────────┘                    │                   │
│  │  Runs task, creates lessons          │                   │
│  └──────────────────────────────────────┘                   │
│         │                                                    │
│         │ extract & merge                                    │
│         ▼                                                    │
│  ┌──────────────┐                                           │
│  │  Master DB   │  ← Now has Task 1 lessons                 │
│  └──────────────┘                                           │
│         │                                                    │
│         │ inject                                             │
│         ▼                                                    │
│  ┌──────────────────────────────────────┐                   │
│  │  Docker Container (Task 2)           │                   │
│  │  ┌──────────────┐                    │                   │
│  │  │  theforge.db │ ← Starts with      │                   │
│  │  │              │   Task 1 lessons!  │                   │
│  │  └──────────────┘                    │                   │
│  │  Runs task, creates more lessons     │                   │
│  └──────────────────────────────────────┘                   │
│         │                                                    │
│         │ extract & merge                                    │
│         ▼                                                    │
│  ┌──────────────┐                                           │
│  │  Master DB   │  ← Now has Task 1+2 lessons               │
│  └──────────────┘                                           │
│         │                                                    │
│        ...                                                   │
└─────────────────────────────────────────────────────────────┘
```

## Quick Start

### Basic Usage

```python
from pathlib import Path
from benchmarks.cumulative_db import CumulativeDB
import docker

# 1. Initialize cumulative DB
master_db = CumulativeDB("/tmp/benchmark_master.db")

# 2. Create Docker container
client = docker.from_env()
container = client.containers.create("equipa:latest", ...)

# 3. Inject master DB into container
master_db.inject_into_container(container, "/app/theforge.db")

# 4. Run the task
container.start()
container.wait()

# 5. Extract and merge DB back to master
master_db.extract_and_merge(container, "/app/theforge.db")

# 6. Check stats
stats = master_db.get_stats()
print(f"Total lessons: {stats['lessons_merged']}")
```

### Integration with FeatureBench

To add cumulative knowledge to `featurebench_docker.py`, add these 3 lines:

```python
# At the start (before task loop)
from benchmarks.cumulative_db import CumulativeDB
cumulative_db = CumulativeDB("/tmp/featurebench_master.db")

# Before starting each container
cumulative_db.inject_into_container(container, "/app/theforge.db")

# After each task completes
cumulative_db.extract_and_merge(container, "/app/theforge.db")
logger.info(f"Cumulative stats: {cumulative_db.get_stats()}")
```

That's it! Now each task learns from all previous tasks.

## API Reference

### `CumulativeDB.__init__(master_db_path: str)`

Initialize with path to master DB on host.

**Args:**
- `master_db_path`: Absolute path to master theforge.db (created if doesn't exist)

### `inject_into_container(container, dest_path: str)`

Copy master DB into Docker container via tar archive.

**Args:**
- `container`: Docker container object
- `dest_path`: Absolute path inside container (e.g., `/app/theforge.db`)

### `extract_and_merge(container, source_path: str)`

Extract DB from container, merge new rows into master.

**Args:**
- `container`: Docker container object
- `source_path`: Absolute path inside container (e.g., `/app/theforge.db`)

**Behavior:**
- Deduplicates `lessons_learned` by content hash
- Appends all other tables (`agent_episodes`, `decisions`, `agent_runs`, `session_notes`)

### `get_stats() -> dict`

Return counts of accumulated rows.

**Returns:**
```python
{
    "lessons_merged": 42,
    "episodes_merged": 150,
    "decisions_merged": 8,
    "runs_merged": 150,
    "notes_merged": 12
}
```

## Tables Merged

| Table | Deduplication | Notes |
|-------|---------------|-------|
| `lessons_learned` | **Yes** (SHA256 hash of `lesson` column) | Prevents duplicate lessons |
| `agent_episodes` | No | Each run is unique |
| `decisions` | No | Track all decisions |
| `agent_runs` | No | Telemetry — all unique |
| `session_notes` | No | Summaries — all unique |

## Features

✅ **Idempotent** — Safe to call multiple times
✅ **Content-based deduplication** — Lessons hashed by content, not ID
✅ **Automatic schema creation** — Creates tables if missing
✅ **Transaction safety** — All merges are atomic
✅ **Logging** — Detailed merge statistics
✅ **Error handling** — Gracefully handles missing tables/files

## Testing

```bash
python3 -m pytest tests/test_cumulative_db.py -v
```

**Test coverage:**
- ✅ Initialization
- ✅ Injection into container
- ✅ Extraction and merge
- ✅ Deduplication
- ✅ Statistics

## Example Output

```
INFO:benchmarks.cumulative_db:Injected master DB (8192 bytes) -> /app/theforge.db
INFO:benchmarks.cumulative_db:Merged container DB (12288 bytes): {
    'lessons_merged': 3,
    'episodes_merged': 1,
    'decisions_merged': 2,
    'runs_merged': 1,
    'notes_merged': 0
}
```

## Files

- `benchmarks/cumulative_db.py` — Core module
- `tests/test_cumulative_db.py` — Comprehensive test suite
- `benchmarks/cumulative_db_example.py` — Usage examples
- `benchmarks/CUMULATIVE_DB_README.md` — This file

## Implementation Notes

### Why content hashing for lessons?

Container DBs use AUTOINCREMENT IDs which can conflict across runs. Content hashing ensures:
- Lesson "Always validate input" from Task 1 is recognized as duplicate in Task 5
- No false duplicates (different lessons with same ID)
- Hash collisions astronomically unlikely (SHA256)

### Why tar archives?

Docker's `put_archive()` and `get_archive()` only support tar format. The module handles tar packing/unpacking transparently.

### Why no deduplication for episodes/decisions?

- **Episodes** are execution traces — each run is unique even for the same task
- **Decisions** should be tracked chronologically, even if revisiting same topic
- **Runs** are telemetry — every invocation matters
- **Notes** are time-specific summaries

Only **lessons** benefit from deduplication because they represent timeless knowledge.

## See Also

- `benchmarks/featurebench_docker.py` — Reference implementation (lines 1079-1098 show DB extraction pattern)
- TheForge schema: `/srv/forge-share/AI_Stuff/TheForge/schema.sql`
