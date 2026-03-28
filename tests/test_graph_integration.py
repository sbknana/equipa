"""Test knowledge graph integration with EQUIPA retrieval pipeline.

Tests PageRank-enhanced episode retrieval, co-accessed edge creation,
and similarity edge auto-linking.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from equipa.constants import THEFORGE_DB
from equipa.db import ensure_schema, get_db_connection
from equipa import graph
from equipa.embeddings import embed_and_store_lesson
from equipa.lessons import (
    get_relevant_episodes,
    update_injected_episode_q_values_for_task,
    _injected_episodes_by_task,
)
from equipa.prompts import build_system_prompt


@pytest.fixture
def clean_db():
    """Reset TheForge DB and ensure schema."""
    ensure_schema()
    conn = get_db_connection(write=True)
    conn.execute("DELETE FROM agent_episodes")
    conn.execute("DELETE FROM lessons_learned")
    conn.execute("DELETE FROM lesson_graph_edges")
    conn.commit()
    conn.close()
    yield
    # Cleanup after test
    conn = get_db_connection(write=True)
    conn.execute("DELETE FROM agent_episodes")
    conn.execute("DELETE FROM lessons_learned")
    conn.execute("DELETE FROM lesson_graph_edges")
    conn.commit()
    conn.close()


def test_graph_reranking_in_episode_retrieval(clean_db):
    """Test that knowledge graph PageRank reranks episodes."""
    conn = get_db_connection(write=True)

    # Create 3 episodes with identical base q_values
    ep_ids = []
    for i in range(1, 4):
        cursor = conn.execute(
            """INSERT INTO agent_episodes
               (task_id, role, task_type, project_id, approach_summary, reflection,
                q_value, outcome, turns_used)
               VALUES (?, 'developer', 'feature', 23, ?, ?, 0.5, 'tests_passed', 10)""",
            (100 + i, f"Approach {i}", f"Reflection {i}"),
        )
        ep_ids.append(cursor.lastrowid)
    conn.commit()
    conn.close()

    # Create graph edges giving episode 3 highest PageRank
    # Episode 1 -> 3, Episode 2 -> 3 (3 has in-degree 2)
    graph.add_edge(ep_ids[0], ep_ids[2], "coaccessed", 1.0)
    graph.add_edge(ep_ids[1], ep_ids[2], "coaccessed", 1.0)

    # Fetch with knowledge_graph enabled
    config = {"features": {"knowledge_graph": True}}
    episodes = get_relevant_episodes(
        role="developer",
        project_id=23,
        task_type="feature",
        min_q_value=0.3,
        limit=3,
        dispatch_config=config,
    )

    # Episode 3 should rank first due to PageRank boost
    assert len(episodes) == 3
    assert episodes[0]["id"] == ep_ids[2], "Episode 3 should rank first (highest PageRank)"


def test_graph_disabled_uses_standard_ranking(clean_db):
    """Test that without knowledge_graph flag, standard ranking is used."""
    conn = get_db_connection(write=True)

    # Create episodes with different q_values
    ep1 = conn.execute(
        """INSERT INTO agent_episodes
           (task_id, role, task_type, project_id, approach_summary, reflection,
            q_value, outcome, turns_used)
           VALUES (101, 'developer', 'feature', 23, 'Low quality', 'Reflection', 0.3, 'tests_passed', 10)"""
    ).lastrowid
    ep2 = conn.execute(
        """INSERT INTO agent_episodes
           (task_id, role, task_type, project_id, approach_summary, reflection,
            q_value, outcome, turns_used)
           VALUES (102, 'developer', 'feature', 23, 'High quality', 'Reflection', 0.9, 'tests_passed', 10)"""
    ).lastrowid
    conn.commit()
    conn.close()

    # Fetch with knowledge_graph disabled
    config = {"features": {"knowledge_graph": False}}
    episodes = get_relevant_episodes(
        role="developer",
        project_id=23,
        task_type="feature",
        min_q_value=0.2,
        limit=2,
        dispatch_config=config,
    )

    # Episode 2 (higher q_value) should rank first
    assert len(episodes) == 2
    assert episodes[0]["id"] == ep2, "Episode 2 should rank first (higher q_value)"


def test_coaccessed_edges_created_on_q_value_update(clean_db):
    """Test that co-accessed edges are created when updating q-values."""
    conn = get_db_connection(write=True)

    # Create 3 episodes
    ep_ids = []
    for i in range(1, 4):
        cursor = conn.execute(
            """INSERT INTO agent_episodes
               (task_id, role, project_id, approach_summary, reflection, q_value, outcome, turns_used)
               VALUES (?, 'developer', 23, ?, ?, 0.5, 'tests_passed', 10)""",
            (100 + i, f"Approach {i}", f"Reflection {i}"),
        )
        ep_ids.append(cursor.lastrowid)
    conn.commit()
    conn.close()

    # Simulate episode injection tracking
    _injected_episodes_by_task[200] = ep_ids

    # Update q-values with knowledge_graph enabled
    config = {"features": {"knowledge_graph": True}}
    update_injected_episode_q_values_for_task(
        task_id=200,
        outcome="tests_passed",
        output=None,
        dispatch_config=config,
    )

    # Check that co-accessed edges were created
    conn = get_db_connection()
    edges = conn.execute("SELECT COUNT(*) FROM lesson_graph_edges WHERE edge_type = 'coaccessed'").fetchone()[0]
    conn.close()

    # 3 episodes -> C(3,2) * 2 = 6 directed edges
    assert edges == 6, f"Expected 6 co-accessed edges, got {edges}"


def test_coaccessed_edges_not_created_when_disabled(clean_db):
    """Test that co-accessed edges are NOT created when feature is disabled."""
    conn = get_db_connection(write=True)

    # Create 2 episodes
    ep_ids = []
    for i in range(1, 3):
        cursor = conn.execute(
            """INSERT INTO agent_episodes
               (task_id, role, project_id, approach_summary, reflection, q_value, outcome, turns_used)
               VALUES (?, 'developer', 23, ?, ?, 0.5, 'tests_passed', 10)""",
            (100 + i, f"Approach {i}", f"Reflection {i}"),
        )
        ep_ids.append(cursor.lastrowid)
    conn.commit()
    conn.close()

    # Simulate episode injection tracking
    _injected_episodes_by_task[201] = ep_ids

    # Update q-values with knowledge_graph disabled
    config = {"features": {"knowledge_graph": False}}
    update_injected_episode_q_values_for_task(
        task_id=201,
        outcome="tests_passed",
        output=None,
        dispatch_config=config,
    )

    # Check that NO edges were created
    conn = get_db_connection()
    edges = conn.execute("SELECT COUNT(*) FROM lesson_graph_edges WHERE edge_type = 'coaccessed'").fetchone()[0]
    conn.close()

    assert edges == 0, f"Expected 0 edges when disabled, got {edges}"


def test_similarity_edges_created_on_lesson_embedding(clean_db):
    """Test that similarity edges are auto-created when embedding a lesson."""
    conn = get_db_connection(write=True)

    # Create 2 lessons
    l1 = conn.execute(
        """INSERT INTO lessons_learned (lesson, error_signature, times_seen, active)
           VALUES ('Lesson A', 'error_a', 1, 1)"""
    ).lastrowid
    l2 = conn.execute(
        """INSERT INTO lessons_learned (lesson, error_signature, times_seen, active)
           VALUES ('Lesson B', 'error_b', 1, 1)"""
    ).lastrowid
    conn.commit()
    conn.close()

    # Mock Ollama to return fixed embeddings
    fake_embedding_a = [0.9, 0.1, 0.0]  # Very similar
    fake_embedding_b = [0.85, 0.15, 0.05]  # cos_sim ≈ 0.98

    call_count = [0]

    def mock_get_embedding(text, model=None, base_url=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return fake_embedding_a
        else:
            return fake_embedding_b

    with patch("equipa.embeddings.get_embedding", side_effect=mock_get_embedding):
        # Embed lesson 1 (no edges yet)
        config = {"features": {"knowledge_graph": True}}
        result1 = embed_and_store_lesson(l1, "Lesson A text", config)
        assert result1 is True

        # Embed lesson 2 (should create similarity edge to lesson 1)
        result2 = embed_and_store_lesson(l2, "Lesson B text", config)
        assert result2 is True

    # Check that similarity edges were created
    conn = get_db_connection()
    edges = conn.execute("SELECT COUNT(*) FROM lesson_graph_edges WHERE edge_type = 'similarity'").fetchone()[0]
    conn.close()

    # Cosine similarity is ~0.98, above threshold 0.8 → 1 edge created (lesson 2 -> lesson 1)
    assert edges >= 1, f"Expected at least 1 similarity edge, got {edges}"


def test_similarity_edges_not_created_when_disabled(clean_db):
    """Test that similarity edges are NOT created when feature is disabled."""
    conn = get_db_connection(write=True)

    # Create 1 lesson
    lx = conn.execute(
        """INSERT INTO lessons_learned (lesson, error_signature, times_seen, active)
           VALUES ('Lesson X', 'error_x', 1, 1)"""
    ).lastrowid
    conn.commit()
    conn.close()

    # Mock Ollama to return embedding
    with patch("equipa.embeddings.get_embedding", return_value=[0.5, 0.5, 0.0]):
        # Embed with knowledge_graph disabled
        config = {"features": {"knowledge_graph": False}}
        result = embed_and_store_lesson(lx, "Lesson X text", config)
        assert result is True

    # Check that NO similarity edges were created
    conn = get_db_connection()
    edges = conn.execute("SELECT COUNT(*) FROM lesson_graph_edges WHERE edge_type = 'similarity'").fetchone()[0]
    conn.close()

    assert edges == 0, f"Expected 0 similarity edges when disabled, got {edges}"


def test_coaccessed_edges_in_prompt_building(clean_db):
    """Test that co-accessed edges are created during prompt building."""
    conn = get_db_connection(write=True)

    # Create project (if not exists)
    try:
        conn.execute(
            """INSERT INTO projects (id, name, path) VALUES (23, 'Test Project', '/test')"""
        )
    except:
        pass  # Project already exists

    # Create task (if not exists)
    try:
        conn.execute(
            """INSERT INTO tasks (id, project_id, title, description, status)
               VALUES (1001, 23, 'Test Task', 'Test description', 'in_progress')"""
        )
    except:
        pass  # Task already exists

    # Create 2 episodes for injection
    ep_ids = []
    for i in range(1, 3):
        cursor = conn.execute(
            """INSERT INTO agent_episodes
               (task_id, role, project_id, approach_summary, reflection, q_value, outcome, turns_used)
               VALUES (?, 'developer', 23, ?, ?, 0.6, 'tests_passed', 10)""",
            (100 + i, f"Approach {i}", f"Reflection {i}"),
        )
        ep_ids.append(cursor.lastrowid)
    conn.commit()
    conn.close()

    # Build system prompt with knowledge_graph enabled
    task = {"id": 1001, "project_id": 23, "title": "Test Task", "description": "Test desc", "task_type": "feature"}
    config = {
        "features": {
            "knowledge_graph": True,
            "forgesmith_episodes": True,
            "forgesmith_lessons": False,
            "language_prompts": False,
        }
    }
    project_context = {}

    prompt = build_system_prompt(
        task=task,
        project_context=project_context,
        project_dir="/test",
        role="developer",
        dispatch_config=config,
    )

    # Check that co-accessed edges were created
    conn = get_db_connection()
    edges = conn.execute("SELECT COUNT(*) FROM lesson_graph_edges WHERE edge_type = 'coaccessed'").fetchone()[0]
    conn.close()

    # 2 episodes injected -> 2 edges (bidirectional: 1->2, 2->1)
    assert edges >= 2, f"Expected at least 2 co-accessed edges from prompt building, got {edges}"


def test_graph_gracefully_handles_empty_adjacency():
    """Test that graph ranking handles empty adjacency lists gracefully."""
    # Empty adjacency → no PageRank scores → falls back to standard ranking
    config = {"features": {"knowledge_graph": True}}
    episodes = get_relevant_episodes(
        role="nonexistent",
        project_id=999,
        task_type="feature",
        min_q_value=0.3,
        limit=3,
        dispatch_config=config,
    )

    # Should return empty list without crashing
    assert episodes == []


def test_graph_handles_import_failure(clean_db):
    """Test that retrieval gracefully handles graph module import failure."""
    conn = get_db_connection(write=True)

    # Create 1 episode
    ep1 = conn.execute(
        """INSERT INTO agent_episodes
           (task_id, role, project_id, approach_summary, reflection, q_value, outcome, turns_used)
           VALUES (101, 'developer', 23, 'Approach', 'Reflection', 0.5, 'tests_passed', 10)"""
    ).lastrowid
    conn.commit()
    conn.close()

    # Mock import to fail
    import sys
    with patch.dict('sys.modules', {'equipa.graph': None}):
        config = {"features": {"knowledge_graph": True}}
        episodes = get_relevant_episodes(
            role="developer",
            project_id=23,
            task_type="feature",
            min_q_value=0.3,
            limit=3,
            dispatch_config=config,
        )

    # Should return episodes using standard ranking (no crash)
    assert len(episodes) == 1
    assert episodes[0]["id"] == ep1


def test_pagerank_boost_overrides_low_qvalue(clean_db):
    """Test that strong PageRank signal can promote low-q_value episodes."""
    conn = get_db_connection(write=True)

    # Episode 1: high q_value, no graph edges
    ep1 = conn.execute(
        """INSERT INTO agent_episodes
           (task_id, role, project_id, approach_summary, reflection, q_value, outcome, turns_used)
           VALUES (101, 'developer', 23, 'High quality', 'Great reflection', 0.9, 'tests_passed', 10)"""
    ).lastrowid
    # Episode 2: low q_value, but highly connected in graph
    ep2 = conn.execute(
        """INSERT INTO agent_episodes
           (task_id, role, project_id, approach_summary, reflection, q_value, outcome, turns_used)
           VALUES (102, 'developer', 23, 'Low quality', 'Weak reflection', 0.4, 'tests_passed', 10)"""
    ).lastrowid
    # Insert pointer episodes
    pointer_ids = []
    for i in range(3, 8):  # 5 episodes pointing to episode 2
        epi = conn.execute(
            """INSERT INTO agent_episodes
               (task_id, role, project_id, approach_summary, reflection, q_value, outcome, turns_used)
               VALUES (?, 'developer', 23, 'Pointer', 'Reflection', 0.5, 'tests_passed', 10)""",
            (100 + i,),
        ).lastrowid
        pointer_ids.append(epi)

    conn.commit()
    conn.close()

    # Create strong graph edges pointing to episode 2 (giving it high PageRank)
    for epi in pointer_ids:
        graph.add_edge(epi, ep2, "coaccessed", 1.0)

    # Fetch with knowledge_graph enabled
    config = {"features": {"knowledge_graph": True}}
    episodes = get_relevant_episodes(
        role="developer",
        project_id=23,
        task_type="feature",
        min_q_value=0.3,
        limit=5,
        dispatch_config=config,
    )

    # Episode 2 (low q_value but high PageRank) should rank higher than without graph
    # It may not be #1 (70% similarity, 30% graph) but should be in top 3
    top_3_ids = [ep["id"] for ep in episodes[:3]]
    assert ep2 in top_3_ids, "Episode 2 should be in top 3 due to PageRank boost"


def test_edge_weight_affects_pagerank():
    """Test that edge weights influence PageRank scores."""
    # Create adjacency with weighted edges
    # Node 1 -> Node 2 (weight 1.0)
    # Node 1 -> Node 3 (weight 10.0)
    # Node 3 should get higher PageRank than Node 2
    adj = {
        1: [(2, 1.0), (3, 10.0)],
    }

    pr_scores = graph.pagerank(adj)

    # Node 3 should have higher score than Node 2 due to higher edge weight
    assert pr_scores[3] > pr_scores[2], "Node 3 should rank higher (10x edge weight)"


def test_multiple_edge_types_in_graph(clean_db):
    """Test that different edge types coexist in the graph."""
    # Create edges of different types
    graph.add_edge(1, 2, "coaccessed", 1.0)
    graph.add_edge(1, 3, "similarity", 0.95)
    graph.add_edge(2, 3, "sequence", 1.0)

    # Fetch adjacency list
    adj = graph.get_adjacency_list()

    # Should include all edge types
    assert 1 in adj
    assert len(adj[1]) == 2  # Node 1 has 2 outgoing edges
    assert 2 in adj
    assert 3 not in adj  # Node 3 has no outgoing edges

    conn = get_db_connection()
    types = set(row[0] for row in conn.execute("SELECT DISTINCT edge_type FROM lesson_graph_edges"))
    conn.close()

    assert types == {"coaccessed", "similarity", "sequence"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
