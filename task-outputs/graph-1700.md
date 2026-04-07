# EQUIPA Knowledge Graph Module — Task 1700

## Summary

Created `equipa/graph.py` with pure Python PageRank, label propagation community detection, and edge management functions for the EQUIPA knowledge graph.

## Files Created

1. **equipa/graph.py** (327 lines)
   - Pure Python PageRank via power iteration
   - Label propagation for community detection
   - Edge management (add, get adjacency list, co-accessed edges)
   - Similarity-based edge creation with cosine similarity
   - Graph-enhanced reranking

2. **tests/test_knowledge_graph.py** (339 lines)
   - 18 comprehensive test cases
   - All tests pass

## Implementation Details

### PageRank Algorithm
- **Damping factor**: 0.85 (Google's original value)
- **Max iterations**: 30
- **Tolerance**: 1e-6 (convergence threshold)
- **Dangling node handling**: Redistributes rank evenly to all nodes
- **Weight-aware**: Uses edge weights in rank distribution

### Label Propagation
- Random shuffle for tie-breaking
- Weighted voting from neighbors
- Max 20 iterations (usually converges faster)
- Returns community assignments as node → community_id mapping

### Edge Management Functions

1. **add_edge(src_id, dst_id, edge_type, weight)**: Creates/updates edges with INSERT OR REPLACE
2. **get_adjacency_list()**: Builds {node_id: [(neighbor, weight)]} from DB
3. **create_coaccessed_edges(lesson_ids)**: Creates bidirectional edges between all pairs
4. **create_similarity_edges(lesson_id, embedding, threshold)**: Finds similar lessons via cosine similarity (threshold default 0.8)

### Graph-Enhanced Ranking

**rerank_with_graph(candidates, pr_scores, sim_weight=0.7, graph_weight=0.3)**
- Blends similarity scores with PageRank centrality
- Default weights: 70% similarity, 30% PageRank
- Returns sorted candidates with `combined_score` field

## Database Integration

Uses existing `lesson_graph_edges` table (created by `db.py` ensure_schema):
```sql
CREATE TABLE lesson_graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id INTEGER NOT NULL,
    dst_id INTEGER NOT NULL,
    edge_type TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(src_id, dst_id, edge_type)
)
```

Indexed on `src_id` and `dst_id` for fast adjacency lookups.

## Test Coverage

All 18 tests pass (2.37s runtime):

### Edge Management (6 tests)
- Add/replace edges
- Adjacency list building
- Co-accessed edge creation (pairs, single node)

### Similarity Edges (2 tests)
- Cosine similarity threshold
- No matches below threshold

### PageRank (5 tests)
- Simple cycle (symmetric rank)
- Hub-and-spoke (hub has highest rank)
- Empty graph
- Dangling node redistribution
- Convergence

### Label Propagation (3 tests)
- Two disconnected components
- Single node
- Empty graph

### Graph-Enhanced Ranking (3 tests)
- Combined score blending
- Similarity-only mode
- Empty PageRank scores

## Usage Example

```python
from equipa.graph import (
    add_edge,
    create_coaccessed_edges,
    create_similarity_edges,
    get_adjacency_list,
    pagerank,
    label_propagation,
    rerank_with_graph,
)

# Create edges
create_coaccessed_edges([1, 2, 3])  # Lessons accessed together
create_similarity_edges(1, embedding_vector, threshold=0.8)

# Compute PageRank
adj = get_adjacency_list()
pr_scores = pagerank(adj, damping=0.85, iterations=30)

# Detect communities
communities = label_propagation(adj, max_iterations=20)

# Rerank candidates
candidates = [
    {"id": 1, "similarity": 0.9},
    {"id": 2, "similarity": 0.8},
]
reranked = rerank_with_graph(candidates, pr_scores)
```

## Performance Characteristics

- **PageRank**: O(|E| × iterations) = O(|E| × 30) typically converges in ~10-15 iterations
- **Label Propagation**: O(|E| × iterations) with random shuffle
- **Cosine Similarity**: O(n × d) where n=lessons, d=embedding_dim
- **Adjacency List**: O(|E|) single query, cached as dict

## Layer 4 Compliance

✓ Imports only from `equipa.db` and `equipa.constants`
✓ No circular dependencies
✓ Pure Python (no external dependencies)
✓ All SQL via parameterized queries
✓ Connection cleanup in every function

## Commits

1. `7acb18d` — feat: add knowledge graph module with PageRank and edge management
2. `6a02ec3` — test: add comprehensive tests for knowledge graph module
3. `8199373` — fix: improve graph module and tests

---

**Implementation complete. All tests pass. Ready for integration.**
