"""Tests for equipa.graph knowledge graph module.

Tests PageRank, label propagation, edge management, and graph-enhanced ranking.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from equipa.constants import THEFORGE_DB
from equipa.db import ensure_schema, get_db_connection
from equipa.graph import (
    add_edge,
    create_coaccessed_edges,
    create_similarity_edges,
    get_adjacency_list,
    label_propagation,
    pagerank,
    rerank_with_graph,
)


@pytest.fixture
def test_db(tmp_path: Path, monkeypatch) -> Path:
    """Create a temporary test database."""
    db_path = tmp_path / "test_graph.db"

    # Override THEFORGE_DB for all modules
    monkeypatch.setattr("equipa.constants.THEFORGE_DB", db_path)
    monkeypatch.setattr("equipa.db.THEFORGE_DB", db_path)

    # Force module reload to pick up new DB path
    import equipa.db
    equipa.db._SCHEMA_ENSURED = False

    # Create schema
    ensure_schema()

    # Create lessons_learned table (not created by ensure_schema)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lessons_learned (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            role TEXT,
            error_type TEXT,
            error_signature TEXT,
            lesson TEXT NOT NULL,
            source TEXT DEFAULT 'forgesmith',
            times_seen INTEGER DEFAULT 1,
            times_injected INTEGER DEFAULT 0,
            effectiveness_score REAL,
            active INTEGER DEFAULT 1,
            embedding TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    # Create some test lessons
    conn.execute(
        """INSERT INTO lessons_learned
           (id, project_id, role, error_type, lesson, embedding)
           VALUES (1, 23, 'developer', 'timeout', 'Lesson 1', ?)""",
        (json.dumps([1.0, 0.0, 0.0]),),
    )
    conn.execute(
        """INSERT INTO lessons_learned
           (id, project_id, role, error_type, lesson, embedding)
           VALUES (2, 23, 'developer', 'syntax', 'Lesson 2', ?)""",
        (json.dumps([0.9, 0.1, 0.0]),),
    )
    conn.execute(
        """INSERT INTO lessons_learned
           (id, project_id, role, error_type, lesson, embedding)
           VALUES (3, 23, 'tester', 'import', 'Lesson 3', ?)""",
        (json.dumps([0.0, 1.0, 0.0]),),
    )
    conn.execute(
        """INSERT INTO lessons_learned
           (id, project_id, role, error_type, lesson, embedding)
           VALUES (4, 23, 'tester', 'assertion', 'Lesson 4', NULL)"""
    )
    conn.commit()
    conn.close()

    yield db_path


def test_add_edge(test_db: Path) -> None:
    """Test adding edges to the graph."""
    add_edge(1, 2, "coaccessed", 1.0)
    add_edge(2, 3, "similarity", 0.9)
    add_edge(1, 3, "sequence", 1.5)

    conn = get_db_connection()
    rows = conn.execute(
        "SELECT src_id, dst_id, edge_type, weight FROM lesson_graph_edges ORDER BY src_id, dst_id"
    ).fetchall()
    conn.close()

    assert len(rows) == 3
    assert rows[0]["src_id"] == 1
    assert rows[0]["dst_id"] == 2
    assert rows[0]["edge_type"] == "coaccessed"
    assert rows[0]["weight"] == 1.0

    assert rows[1]["src_id"] == 1
    assert rows[1]["dst_id"] == 3
    assert rows[1]["edge_type"] == "sequence"
    assert rows[1]["weight"] == 1.5

    assert rows[2]["src_id"] == 2
    assert rows[2]["dst_id"] == 3
    assert rows[2]["edge_type"] == "similarity"
    assert rows[2]["weight"] == 0.9


def test_add_edge_replace(test_db: Path) -> None:
    """Test that duplicate edges are replaced with new weight."""
    add_edge(1, 2, "coaccessed", 1.0)
    add_edge(1, 2, "coaccessed", 2.5)

    conn = get_db_connection()
    rows = conn.execute(
        "SELECT weight FROM lesson_graph_edges WHERE src_id = 1 AND dst_id = 2"
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]["weight"] == 2.5


def test_get_adjacency_list(test_db: Path) -> None:
    """Test building adjacency list from edges."""
    add_edge(1, 2, "coaccessed", 1.0)
    add_edge(1, 3, "similarity", 0.8)
    add_edge(2, 3, "sequence", 1.5)
    add_edge(3, 1, "coaccessed", 1.0)

    adj = get_adjacency_list()

    assert len(adj) == 3
    assert set(adj.keys()) == {1, 2, 3}
    assert adj[1] == [(2, 1.0), (3, 0.8)]
    assert adj[2] == [(3, 1.5)]
    assert adj[3] == [(1, 1.0)]


def test_get_adjacency_list_empty(test_db: Path) -> None:
    """Test adjacency list with no edges."""
    adj = get_adjacency_list()
    assert adj == {}


def test_create_coaccessed_edges(test_db: Path) -> None:
    """Test creating co-accessed edges between lesson pairs."""
    created = create_coaccessed_edges([1, 2, 3])

    assert created == 6  # 3 pairs, bidirectional

    conn = get_db_connection()
    rows = conn.execute(
        "SELECT src_id, dst_id FROM lesson_graph_edges ORDER BY src_id, dst_id"
    ).fetchall()
    conn.close()

    assert len(rows) == 6
    # Pairs: (1,2), (2,1), (1,3), (3,1), (2,3), (3,2)
    edges = {(r["src_id"], r["dst_id"]) for r in rows}
    assert edges == {(1, 2), (2, 1), (1, 3), (3, 1), (2, 3), (3, 2)}


def test_create_coaccessed_edges_single_lesson(test_db: Path) -> None:
    """Test co-accessed edges with only one lesson (should create nothing)."""
    created = create_coaccessed_edges([1])
    assert created == 0

    conn = get_db_connection()
    rows = conn.execute("SELECT COUNT(*) as cnt FROM lesson_graph_edges").fetchone()
    conn.close()
    assert rows["cnt"] == 0


def test_create_similarity_edges(test_db: Path) -> None:
    """Test creating similarity edges based on embedding cosine similarity."""
    # Lesson 1: [1.0, 0.0, 0.0]
    # Lesson 2: [0.9, 0.1, 0.0] → similarity with 1 ≈ 0.995 (above 0.8)
    # Lesson 3: [0.0, 1.0, 0.0] → similarity with 1 = 0.0 (below 0.8)
    # Lesson 4: no embedding

    created = create_similarity_edges(1, [1.0, 0.0, 0.0], threshold=0.8)

    assert created == 1  # Only lesson 2 is similar enough

    conn = get_db_connection()
    rows = conn.execute(
        "SELECT dst_id, weight FROM lesson_graph_edges WHERE src_id = 1"
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]["dst_id"] == 2
    assert rows[0]["weight"] > 0.99  # Cosine similarity ≈ 0.995


def test_create_similarity_edges_no_matches(test_db: Path) -> None:
    """Test similarity edges when no lessons meet threshold."""
    created = create_similarity_edges(3, [0.0, 1.0, 0.0], threshold=0.99)
    assert created == 0


def test_pagerank_simple_graph(test_db: Path) -> None:
    """Test PageRank on a simple directed graph."""
    # Graph: 1 -> 2, 2 -> 3, 3 -> 1 (cycle)
    add_edge(1, 2, "test", 1.0)
    add_edge(2, 3, "test", 1.0)
    add_edge(3, 1, "test", 1.0)

    adj = get_adjacency_list()
    scores = pagerank(adj, damping=0.85, iterations=30)

    # All nodes should have equal rank in a symmetric cycle
    assert len(scores) == 3
    assert abs(scores[1] - scores[2]) < 0.01
    assert abs(scores[2] - scores[3]) < 0.01
    assert abs(scores[1] + scores[2] + scores[3] - 1.0) < 0.01  # Sum to 1


def test_pagerank_hub_and_spoke(test_db: Path) -> None:
    """Test PageRank with hub node (node 1 has all incoming edges)."""
    # Graph: 2 -> 1, 3 -> 1, 4 -> 1
    add_edge(2, 1, "test", 1.0)
    add_edge(3, 1, "test", 1.0)
    add_edge(4, 1, "test", 1.0)

    adj = get_adjacency_list()
    scores = pagerank(adj, damping=0.85, iterations=30)

    # Node 1 should have highest rank (all edges point to it)
    assert scores[1] > scores[2]
    assert scores[1] > scores[3]
    assert scores[1] > scores[4]


def test_pagerank_empty_graph(test_db: Path) -> None:
    """Test PageRank on empty graph."""
    scores = pagerank({})
    assert scores == {}


def test_pagerank_dangling_node(test_db: Path) -> None:
    """Test PageRank with dangling node (no outgoing edges)."""
    # Graph: 1 -> 2, but 2 has no outgoing edges
    add_edge(1, 2, "test", 1.0)

    adj = get_adjacency_list()
    scores = pagerank(adj, damping=0.85, iterations=30)

    # Both nodes should have non-zero rank (dangling node redistributes)
    assert scores[1] > 0
    assert scores[2] > 0
    # Node 2 receives more rank since node 1 points to it
    assert scores[2] > scores[1]


def test_label_propagation_two_components(test_db: Path) -> None:
    """Test label propagation with two disconnected components."""
    # Component 1: 1 <-> 2
    # Component 2: 3 <-> 4
    add_edge(1, 2, "test", 1.0)
    add_edge(2, 1, "test", 1.0)
    add_edge(3, 4, "test", 1.0)
    add_edge(4, 3, "test", 1.0)

    adj = get_adjacency_list()
    labels = label_propagation(adj, max_iterations=20)

    # Nodes in the same component should have the same label
    assert labels[1] == labels[2]
    assert labels[3] == labels[4]
    assert labels[1] != labels[3]


def test_label_propagation_single_node(test_db: Path) -> None:
    """Test label propagation with isolated node."""
    add_edge(1, 2, "test", 1.0)

    adj = get_adjacency_list()
    labels = label_propagation(adj, max_iterations=20)

    # Should have labels for all nodes (1, 2)
    assert 1 in labels
    assert 2 in labels


def test_label_propagation_empty(test_db: Path) -> None:
    """Test label propagation on empty graph."""
    labels = label_propagation({})
    assert labels == {}


def test_rerank_with_graph(test_db: Path) -> None:
    """Test graph-enhanced reranking."""
    candidates = [
        {"id": 1, "similarity": 0.9},
        {"id": 2, "similarity": 0.8},
        {"id": 3, "similarity": 0.7},
    ]
    pr_scores = {1: 0.2, 2: 0.5, 3: 0.3}  # Node 2 has highest PageRank

    reranked = rerank_with_graph(candidates, pr_scores, sim_weight=0.5, graph_weight=0.5)

    # Node 2 should rank highest (0.8 * 0.5 + 1.0 * 0.5 = 0.9)
    # Node 1: 0.9 * 0.5 + 0.4 * 0.5 = 0.65
    # Node 3: 0.7 * 0.5 + 0.6 * 0.5 = 0.65
    assert reranked[0]["id"] == 2
    assert reranked[0]["combined_score"] == pytest.approx(0.9, abs=0.01)


def test_rerank_with_graph_sim_only(test_db: Path) -> None:
    """Test reranking with only similarity (graph_weight=0)."""
    candidates = [
        {"id": 1, "similarity": 0.7},
        {"id": 2, "similarity": 0.9},
        {"id": 3, "similarity": 0.8},
    ]
    pr_scores = {1: 1.0, 2: 0.0, 3: 0.5}

    reranked = rerank_with_graph(candidates, pr_scores, sim_weight=1.0, graph_weight=0.0)

    # Should sort by similarity only
    assert reranked[0]["id"] == 2
    assert reranked[1]["id"] == 3
    assert reranked[2]["id"] == 1


def test_rerank_with_graph_empty_pr(test_db: Path) -> None:
    """Test reranking when PageRank scores are empty."""
    candidates = [
        {"id": 1, "similarity": 0.9},
        {"id": 2, "similarity": 0.8},
    ]
    pr_scores = {}

    reranked = rerank_with_graph(candidates, pr_scores)

    # Should still sort by similarity
    assert reranked[0]["id"] == 1
    assert reranked[1]["id"] == 2
