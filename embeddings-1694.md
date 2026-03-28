# Task 1694 — EQUIPA Enhancement: embeddings.py

**Status:** COMPLETE
**Commit:** e6a06bb
**Date:** 2026-03-28

## Summary

Created `equipa/embeddings.py` (Layer 3) with Ollama vector embedding client and cosine similarity search. Zero pip dependencies, stdlib-only implementation using `urllib.request`.

## Implementation

### Core Functions

1. **`cosine_similarity(a, b)`** — Pure Python cosine similarity (~10 lines)
   - Returns value in [-1, 1]
   - Handles mismatched dimensions, empty vectors, zero-magnitude vectors gracefully

2. **`get_embedding(text, model, base_url)`** — Ollama API client
   - Uses `urllib.request` with 5s timeout
   - Default model: `all-MiniLM-L6-v2` (384-dim)
   - Returns `list[float] | None` on failure (Ollama down, timeout, API error)

3. **`embed_and_store_lesson(lesson_id, text, dispatch_config)`**
   - Generates embedding via Ollama
   - Stores as JSON in `lessons.embedding` column
   - Returns `bool` success status

4. **`embed_and_store_episode(episode_id, text, dispatch_config)`**
   - Same pattern for episodes table
   - Returns `bool` success status

5. **`find_similar_by_embedding(query_text, table, top_k, dispatch_config)`**
   - Brute-force cosine similarity search
   - Works on `lessons` or `episodes` tables
   - Returns `list[tuple[int, float]]` sorted by descending similarity
   - Returns empty list on Ollama failure or DB error

### Graceful Degradation

All functions handle failures without raising exceptions:
- Ollama down/unreachable → `None` / `False` / `[]`
- Database errors → `False` / `[]`
- Invalid inputs → `0.0` / `None` / `[]`
- Malformed JSON in DB → skipped silently

### Configuration

Functions accept optional `dispatch_config` dict with keys:
- `ollama_model` (default: `"all-MiniLM-L6-v2"`)
- `ollama_base_url` (default: `"http://localhost:11434"`)

### Integration

- Added to `equipa/__init__.py` exports (5 functions)
- Layer 3 module: imports only from `equipa.constants` (THEFORGE_DB)
- Zero external dependencies (stdlib `urllib.request`, `json`, `sqlite3`)

## Tests

Created `tests/test_embeddings.py` with **23 tests** covering:

- **Cosine similarity** (6 tests): identical/orthogonal/opposite vectors, edge cases
- **get_embedding** (5 tests): successful API call, empty text, Ollama down, timeout, defaults
- **embed_and_store_lesson** (4 tests): success, Ollama failure, DB error, custom config
- **embed_and_store_episode** (2 tests): success, Ollama failure
- **find_similar_by_embedding** (6 tests): successful search, invalid table, Ollama/DB failures, malformed JSON, top_k limit

All tests use `unittest.mock` to mock `urllib.request.urlopen` and `sqlite3.connect`.

**Test result:** 23/23 passed in 0.11s ✅

## Files Changed

- `equipa/embeddings.py` (new, 200 LOC)
- `equipa/__init__.py` (added 5 exports)
- `tests/test_embeddings.py` (new, 276 LOC)

## Dependencies

**Zero pip dependencies added.** Uses only Python stdlib:
- `urllib.request` (HTTP client)
- `json` (JSON encoding/decoding)
- `sqlite3` (database access)
- `typing` (type hints)

## Next Steps

1. **Migration:** Add `embedding TEXT` column to `lessons` and `episodes` tables (Task 1695)
2. **Wire into retrieval:** Integrate `find_similar_by_embedding()` into lesson/episode retrieval pipeline (Task 1696)
3. **Backfill:** Run batch embedding job on existing lessons/episodes
4. **Monitoring:** Add vector memory hit/miss telemetry to dashboard

## Performance Notes

- **Brute-force search** is acceptable for <1000 lessons
- Scales O(n) where n = row count
- If dataset grows >10K, consider:
  - Pre-filtering by project_id before cosine search
  - Caching query embeddings
  - Upgrading to FAISS/HNSW index (but requires pip dependency)

## API Example

```python
from equipa.embeddings import (
    cosine_similarity,
    get_embedding,
    find_similar_by_embedding,
    embed_and_store_lesson,
)

# Basic usage
embedding = get_embedding("test description")  # → [0.1, 0.2, ..., 0.384]

# Similarity search
similar = find_similar_by_embedding(
    query_text="authentication bug",
    table="lessons",
    top_k=5,
)
# → [(42, 0.87), (103, 0.76), (88, 0.64), ...]

# Store lesson embedding
success = embed_and_store_lesson(
    lesson_id=123,
    text="Recurring error: connection timeout",
)
# → True

# Custom Ollama config
config = {
    "ollama_model": "nomic-embed-text",
    "ollama_base_url": "http://gpu-server:11434",
}
embed_and_store_lesson(123, "lesson text", dispatch_config=config)
```

---

**RESULT:** success
**SUMMARY:** Created equipa/embeddings.py with Ollama client, cosine similarity, and DB storage functions
**FILES_CHANGED:**
- equipa/embeddings.py
- equipa/__init__.py
- tests/test_embeddings.py
- embeddings-1694.md

**DECISIONS:** Used brute-force cosine search (acceptable for <1000 lessons). Zero pip dependencies via urllib.request. Graceful degradation on all failures.

**BLOCKERS:** none

**REFLECTION:** Task was straightforward — implemented Ollama client with urllib.request (avoided requests lib to maintain zero-dependency requirement), wrote pure Python cosine similarity function, and added DB storage functions following existing equipa patterns (get_db_connection from constants.THEFORGE_DB). Tests with unittest.mock verified all code paths including failure modes. All 23 tests passed on first run. The 5-second timeout on urlopen strikes the right balance between responsiveness and allowing Ollama to respond on slower hardware. Next task (DB migration) will add the embedding column to enable actual storage.
