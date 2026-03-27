# Database Migration v5 - Task 1692

## Overview
Added schema migration v5 to support embedding storage and lesson graph relationships.

## Changes Made

### 1. Migration Script (`db_migrate.py`)
- Updated `CURRENT_VERSION` to 5
- Added `migrate_v4_to_v5()` function that:
  - Adds `embedding TEXT` column to `lessons_learned` table
  - Adds `embedding TEXT` column to `agent_episodes` table
  - Creates new `lesson_graph_edges` table with:
    - `id INTEGER PRIMARY KEY AUTOINCREMENT`
    - `src_id INTEGER NOT NULL` (references lessons_learned)
    - `dst_id INTEGER NOT NULL` (references lessons_learned)
    - `edge_type TEXT NOT NULL` (relationship type)
    - `weight REAL DEFAULT 0.0` (edge weight/strength)
    - `created_at TEXT NOT NULL` (ISO 8601 timestamp)
    - `UNIQUE(src_id, dst_id, edge_type)` constraint
  - Creates indexes on `src_id` and `dst_id` for efficient graph queries
- All ALTER TABLE operations wrapped in try/except for idempotency

### 2. Schema Definition (`schema.sql`)
- Added `embedding TEXT` column to `lessons_learned` table
- Added `embedding TEXT` column to `agent_episodes` table
- Added complete `lesson_graph_edges` table definition
- Added indexes: `idx_graph_src`, `idx_graph_dst`

### 3. Database Module (`equipa/db.py`)
- Updated `ensure_schema()` to include v5 schema elements
- Added embedding columns to both tables
- Added lesson_graph_edges table creation
- Added graph indexes

### 4. Test Suite (`tests/test_db_migration_v5.py`)
- 8 comprehensive tests covering:
  - Embedding column creation in both tables
  - Graph table creation with correct schema
  - Index creation on graph table
  - Migration idempotency (safe to run multiple times)
  - Graph edge insertion functionality
  - Unique constraint enforcement on (src_id, dst_id, edge_type)
  - Full v4→v5 migration path
  - Embedding JSON storage capability

## Test Results
All 8 tests passed successfully in 0.68s:
- `test_migration_adds_embedding_columns` ✓
- `test_migration_creates_graph_table` ✓
- `test_migration_creates_indexes` ✓
- `test_migration_idempotent` ✓
- `test_graph_edge_insert` ✓
- `test_graph_unique_constraint` ✓
- `test_full_migration_v4_to_v5` ✓
- `test_embedding_can_store_json` ✓

## Usage
The migration runs automatically when:
1. Creating a new database via `ensure_schema()`
2. Opening an existing database via `run_migrations()`

Embedding columns store TEXT (JSON-serialized vectors), and the graph table enables relationship tracking between lessons for future semantic search and knowledge graph features.

## Files Modified
- `db_migrate.py` - Added v5 migration function
- `schema.sql` - Added embedding columns and graph table
- `equipa/db.py` - Updated ensure_schema with v5 elements
- `tests/test_db_migration_v5.py` - New test file with 8 tests
