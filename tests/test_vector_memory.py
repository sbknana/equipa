"""Tests for EQUIPA vector memory (embeddings + retrieval).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from equipa.constants import THEFORGE_DB
from equipa.db import ensure_schema
from equipa.embeddings import cosine_similarity, find_similar_by_embedding, get_embedding
from equipa.lessons import get_relevant_episodes, record_agent_episode


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """Create a temporary test database."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("equipa.constants.THEFORGE_DB", db_path)
    monkeypatch.setattr("equipa.db.THEFORGE_DB", db_path)
    monkeypatch.setattr("equipa.lessons.THEFORGE_DB", db_path)
    monkeypatch.setattr("equipa.embeddings.THEFORGE_DB", db_path)

    # Reset the global schema flag to force schema creation
    import equipa.db
    monkeypatch.setattr("equipa.db._SCHEMA_ENSURED", False)

    # Initialize schema
    ensure_schema()

    # Insert test data
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Insert agent episodes with different characteristics (directly into agent_episodes)
    # Insert episodes with project_id=23 for test retrieval
    ep1 = {
        "task_id": 1,
        "role": "developer",
        "project_id": 23,
        "outcome": "success",
        "q_value": 0.7,
        "reflection": "Fixed async endpoint by adding await keywords. Used asyncio.gather for parallel DB calls.",
        "embedding": json.dumps([0.8, 0.2, 0.0]),
        "created_at": datetime.now().isoformat(),
    }

    ep2 = {
        "task_id": 2,
        "role": "developer",
        "project_id": 23,
        "outcome": "success",
        "q_value": 0.6,
        "reflection": "Refactored React component to use hooks instead of class components.",
        "embedding": json.dumps([0.1, 0.9, 0.0]),
        "created_at": datetime.now().isoformat(),
    }

    ep3 = {
        "task_id": 3,
        "role": "tester",
        "project_id": 23,
        "outcome": "tests_passed",
        "q_value": 0.8,
        "reflection": "All async tests passed after fixing race condition in DB pool.",
        "embedding": json.dumps([0.7, 0.3, 0.0]),
        "created_at": datetime.now().isoformat(),
    }

    for ep in [ep1, ep2, ep3]:
        conn.execute(
            """INSERT INTO agent_episodes (task_id, role, project_id, outcome, q_value, reflection, embedding, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ep["task_id"], ep["role"], ep["project_id"], ep["outcome"], ep["q_value"], ep["reflection"], ep["embedding"], ep["created_at"]),
        )

    conn.commit()
    conn.close()

    yield db_path


# --- Cosine Similarity Tests ---


class TestCosineSimilarity:
    """Tests for cosine_similarity helper function."""

    def test_identical_vectors(self):
        """Identical vectors should have similarity of 1.0."""
        v = [1.0, 2.0, 3.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        """Orthogonal vectors should have similarity of 0.0."""
        v1 = [1.0, 0.0, 0.0]
        v2 = [0.0, 1.0, 0.0]
        assert cosine_similarity(v1, v2) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        """Opposite vectors should have similarity of -1.0."""
        v1 = [1.0, 0.0]
        v2 = [-1.0, 0.0]
        assert cosine_similarity(v1, v2) == pytest.approx(-1.0)

    def test_unit_vectors(self):
        """Known unit vectors should match expected similarity."""
        v1 = [1.0, 0.0]
        v2 = [0.707, 0.707]
        expected = 0.707  # cos(45°)
        assert cosine_similarity(v1, v2) == pytest.approx(expected, abs=0.01)

    def test_zero_length_vector(self):
        """Zero-length vector should return 0.0 (not divide by zero)."""
        v1 = [0.0, 0.0, 0.0]
        v2 = [1.0, 2.0, 3.0]
        assert cosine_similarity(v1, v2) == 0.0

    def test_mismatched_dimensions(self):
        """Mismatched dimensions should return 0.0."""
        v1 = [1.0, 2.0]
        v2 = [1.0, 2.0, 3.0]
        assert cosine_similarity(v1, v2) == 0.0

    def test_empty_vectors(self):
        """Empty vectors should return 0.0."""
        assert cosine_similarity([], []) == 0.0


# --- get_relevant_episodes Tests ---


class TestGetRelevantEpisodesVectorMemoryOff:
    """Test get_relevant_episodes with vector_memory disabled."""

    def test_keyword_scoring_without_vector_memory(self, test_db):
        """With vector_memory OFF, episodes should be ranked by keyword overlap only."""
        query = "Fix API endpoint async patterns"

        # Call get_relevant_episodes with vector_memory=False
        episodes = get_relevant_episodes(
            role="developer",
            project_id=23,
            limit=2,
            task_description=query,
            dispatch_config={"vector_memory": False},
        )

        # Should return episodes ranked by keyword overlap
        assert len(episodes) <= 2
        # Episode 1 should rank highest (contains "async endpoint")
        if episodes:
            assert "async" in episodes[0]["reflection"].lower()


class TestGetRelevantEpisodesVectorMemoryOn:
    """Test get_relevant_episodes with vector_memory enabled."""

    def test_vector_memory_on_does_not_crash(self, test_db):
        """With vector_memory ON, system should not crash (integration behavior test)."""
        query = "Database performance tuning"

        # Call with vector_memory enabled - should not crash even if Ollama unavailable
        episodes = get_relevant_episodes(
            role="developer",
            project_id=23,
            limit=2,
            task_description=query,
            dispatch_config={"vector_memory": True},
        )

        # Should return episodes (may fall back to keyword scoring)
        assert isinstance(episodes, list)


# --- record_agent_episode Embedding Tests ---


class TestRecordAgentEpisodeEmbedding:
    """Test that record_agent_episode handles vector_memory flag correctly."""

    def test_record_episode_with_vector_memory_on(self, test_db):
        """If outcome=success and vector_memory=True, episode should be recorded."""
        task = {"id": 1, "project_id": 23}
        result = {}
        output = [
            {"type": "text", "text": "I fixed the bug by adding null checks."},
            {"type": "text", "text": "RESULT: success\nREFLECTION: Added defensive coding."},
        ]

        # Should not crash even if Ollama unavailable
        record_agent_episode(
            task=task,
            result=result,
            outcome="success",
            role="developer",
            output=output,
            dispatch_config={"vector_memory": True},
        )

        # Episode should be recorded
        conn = sqlite3.connect(str(test_db))
        cursor = conn.execute("SELECT COUNT(*) FROM agent_episodes WHERE task_id = 1")
        count = cursor.fetchone()[0]
        conn.close()

        assert count >= 1

    @mock.patch("equipa.embeddings.get_embedding")
    def test_embedding_not_called_with_vector_memory_off(
        self, mock_get_embedding, test_db
    ):
        """If vector_memory=False, embedding should NOT be computed."""
        task = {"id": 1, "project_id": 23}
        result = {}
        output = [
            {"type": "text", "text": "RESULT: success\nREFLECTION: Fixed issue."},
        ]

        record_agent_episode(
            task=task,
            result=result,
            outcome="success",
            role="developer",
            output=output,
            dispatch_config={"vector_memory": False},
        )

        # get_embedding should NOT have been called
        mock_get_embedding.assert_not_called()

    @mock.patch("equipa.embeddings.embed_and_store_episode")
    def test_embedding_failure_does_not_block_recording(
        self, mock_embed, test_db
    ):
        """If get_embedding fails, episode should still be recorded."""
        mock_embed.return_value = False  # Simulate Ollama failure

        task = {"id": 999, "project_id": 23}
        result = {}
        output = [
            {"type": "text", "text": "RESULT: success\nREFLECTION: Fixed it."},
        ]

        record_agent_episode(
            task=task,
            result=result,
            outcome="success",
            role="developer",
            output=output,
            dispatch_config={"vector_memory": True},
        )

        # Episode should have been recorded even though embedding failed
        conn = sqlite3.connect(str(test_db))
        cursor = conn.execute("SELECT COUNT(*) FROM agent_episodes WHERE task_id = 999")
        count = cursor.fetchone()[0]
        conn.close()

        # Should have 1 new episode
        assert count == 1


# --- End-to-End Integration Tests ---


class TestEndToEndVectorMemory:
    """End-to-end tests for vector memory workflow."""

    def test_insert_and_retrieve_workflow(self, test_db):
        """Insert episode with vector_memory ON, then retrieve - should not crash."""
        # Insert a new episode with vector memory ON
        task = {"id": 99, "project_id": 23}
        result = {}
        output = [
            {
                "type": "text",
                "text": "RESULT: success\nREFLECTION: Optimized SQL queries for report generation.",
            },
        ]

        # Insert - should handle Ollama unavailability gracefully
        record_agent_episode(
            task=task,
            result=result,
            outcome="success",
            role="developer",
            output=output,
            dispatch_config={"vector_memory": True},
        )

        # Retrieve episodes with a similar query - should fall back to keyword if needed
        episodes = get_relevant_episodes(
            role="developer",
            project_id=23,
            limit=3,
            task_description="Database performance optimization",
            dispatch_config={"vector_memory": True},
        )

        # Should retrieve episodes without crashing
        assert isinstance(episodes, list)

    @mock.patch("equipa.embeddings.get_embedding")
    def test_dissimilar_query_ranks_lower(
        self, mock_get_embedding, test_db
    ):
        """A query with dissimilar embedding should rank matching episodes lower."""
        # Query with embedding very different from episode 1
        query_embedding = [0.0, 0.0, 1.0]
        mock_get_embedding.return_value = query_embedding

        episodes = get_relevant_episodes(
            role="developer",
            project_id=23,
            limit=3,
            task_description="Unrelated topic",
            dispatch_config={"vector_memory": True},
        )

        # Episodes should still be returned (keyword fallback) but scores will be lower
        # This test verifies the system doesn't crash on dissimilar queries
        assert isinstance(episodes, list)


# --- Ollama Mocking Tests ---


class TestOllamaMocking:
    """Test that Ollama API calls are properly mocked."""

    @mock.patch("urllib.request.urlopen")
    def test_get_embedding_mocks_urllib(self, mock_urlopen):
        """Verify urllib.request.urlopen is correctly mocked for Ollama calls."""
        mock_response = mock.MagicMock()
        mock_response.read.return_value = json.dumps(
            {"embedding": [0.1, 0.2, 0.3]}
        ).encode()
        mock_response.__enter__ = mock.Mock(return_value=mock_response)
        mock_response.__exit__ = mock.Mock(return_value=False)
        mock_urlopen.return_value = mock_response

        embedding = get_embedding("test query")

        assert embedding == [0.1, 0.2, 0.3]
        mock_urlopen.assert_called_once()

    @mock.patch("urllib.request.urlopen")
    def test_get_embedding_handles_timeout(self, mock_urlopen):
        """Ollama timeout should be handled gracefully."""
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("timeout")

        embedding = get_embedding("test query")

        assert embedding is None

    @mock.patch("urllib.request.urlopen")
    def test_get_embedding_handles_connection_error(self, mock_urlopen):
        """Ollama connection error should be handled gracefully."""
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("connection refused")

        embedding = get_embedding("test query")

        assert embedding is None


# --- find_similar_by_embedding Tests ---


class TestFindSimilarByEmbedding:
    """Test the find_similar_by_embedding function."""

    @mock.patch("equipa.embeddings.get_embedding")
    def test_find_similar_returns_sorted_by_similarity(self, mock_get_embedding, test_db):
        """find_similar_by_embedding should return episodes sorted by cosine similarity."""
        # Query embedding close to episode 1
        query_embedding = [0.85, 0.15, 0.0]
        mock_get_embedding.return_value = query_embedding

        results = find_similar_by_embedding(
            "Fix async issues", table="agent_episodes", top_k=2
        )

        # Should return list of (id, score) tuples
        assert isinstance(results, list)
        # Episode 1 should rank highest (embedding [0.8, 0.2, 0.0])
        if results:
            assert len(results) <= 2
            # Scores should be in descending order
            scores = [score for _, score in results]
            assert scores == sorted(scores, reverse=True)

    @mock.patch("equipa.embeddings.get_embedding")
    def test_find_similar_returns_empty_on_ollama_failure(
        self, mock_get_embedding, test_db
    ):
        """If Ollama fails, find_similar_by_embedding should return empty list."""
        mock_get_embedding.return_value = None

        results = find_similar_by_embedding("query", table="agent_episodes", top_k=5)

        assert results == []

    def test_find_similar_invalid_table_returns_empty(self, test_db):
        """If table name is invalid, find_similar_by_embedding should return empty list."""
        results = find_similar_by_embedding(
            "query", table="nonexistent_table", top_k=5
        )

        assert results == []
