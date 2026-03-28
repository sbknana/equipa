"""End-to-end tests for EQUIPA vector memory system.

Tests cosine similarity, episode retrieval with/without vector memory enabled,
embedding generation, and full integration flow.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest.mock as mock
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from equipa.db import ensure_schema, get_db_connection
from equipa.embeddings import (
    cosine_similarity,
    embed_and_store_episode,
    find_similar_by_embedding,
    get_embedding,
)
from equipa.lessons import get_relevant_episodes, record_agent_episode


@pytest.fixture(scope="module")
def test_db():
    """Create a temporary test database for all tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_db_path = Path(f.name)

    # Monkey-patch THEFORGE_DB to point to test database
    import equipa.constants
    original_db = equipa.constants.THEFORGE_DB
    equipa.constants.THEFORGE_DB = test_db_path

    # Also patch in other modules
    import equipa.db
    import equipa.embeddings
    import equipa.lessons
    equipa.db.THEFORGE_DB = test_db_path
    equipa.embeddings.THEFORGE_DB = test_db_path
    equipa.lessons.THEFORGE_DB = test_db_path

    ensure_schema()

    yield test_db_path

    # Cleanup
    equipa.constants.THEFORGE_DB = original_db
    equipa.db.THEFORGE_DB = original_db
    equipa.embeddings.THEFORGE_DB = original_db
    equipa.lessons.THEFORGE_DB = original_db
    test_db_path.unlink(missing_ok=True)


# ============================================================================
# Test Suite 1: Cosine Similarity Unit Tests
# ============================================================================


class TestCosineSimilarity:
    """Unit tests for the cosine_similarity function."""

    def test_identical_vectors(self):
        """Identical vectors should have similarity of 1.0."""
        v1 = [1.0, 2.0, 3.0]
        v2 = [1.0, 2.0, 3.0]
        assert cosine_similarity(v1, v2) == 1.0

    def test_orthogonal_vectors(self):
        """Orthogonal vectors should have similarity of 0.0."""
        v1 = [1.0, 0.0]
        v2 = [0.0, 1.0]
        assert abs(cosine_similarity(v1, v2)) < 1e-9

    def test_opposite_vectors(self):
        """Opposite vectors should have similarity of -1.0."""
        v1 = [1.0, 0.0]
        v2 = [-1.0, 0.0]
        assert cosine_similarity(v1, v2) == -1.0

    def test_unit_vectors(self):
        """Known unit vectors should match expected similarity."""
        v1 = [1.0, 0.0, 0.0]
        v2 = [0.5, 0.866, 0.0]  # 60-degree angle
        similarity = cosine_similarity(v1, v2)
        assert abs(similarity - 0.5) < 0.01

    def test_zero_length_vector(self):
        """Zero-length vector should return 0.0 similarity."""
        v1 = [1.0, 2.0]
        v2 = [0.0, 0.0]
        assert cosine_similarity(v1, v2) == 0.0

    def test_mismatched_dimensions(self):
        """Mismatched dimensions should return 0.0 similarity."""
        v1 = [1.0, 2.0, 3.0]
        v2 = [1.0, 2.0]
        assert cosine_similarity(v1, v2) == 0.0

    def test_empty_vectors(self):
        """Empty vectors should return 0.0 similarity."""
        v1: list[float] = []
        v2: list[float] = []
        assert cosine_similarity(v1, v2) == 0.0


# ============================================================================
# Test Suite 2: get_relevant_episodes with vector_memory OFF
# ============================================================================


class TestGetRelevantEpisodesVectorMemoryOff:
    """Test that get_relevant_episodes falls back to keyword scoring when vector_memory is OFF."""

    @pytest.fixture(autouse=True)
    def setup_db(self, test_db):
        """Set up test database with sample episodes."""
        conn = get_db_connection(write=True)

        # Clear existing episodes
        conn.execute("DELETE FROM agent_episodes")

        # Insert episodes with different keyword overlap
        conn.execute(
            """
            INSERT INTO agent_episodes (task_id, role, outcome, reflection, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                1,
                "developer",
                "success",
                "Successfully refactored the API endpoint using async/await patterns.",
                datetime.now().isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_episodes (task_id, role, outcome, reflection, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                2,
                "developer",
                "success",
                "Fixed database connection pooling issue in production.",
                datetime.now().isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_episodes (task_id, role, outcome, reflection, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                3,
                "reviewer",
                "success",
                "Reviewed security vulnerabilities in authentication flow.",
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        conn.close()

    def test_keyword_scoring_without_vector_memory(self, test_db):
        """With vector_memory OFF, episodes should be ranked by keyword overlap only."""
        query = "Fix API endpoint async patterns"

        # Call get_relevant_episodes with vector_memory=False
        episodes = get_relevant_episodes(
            query, role_filter="developer", limit=2, vector_memory=False
        )

        # Should return 2 developer episodes
        assert len(episodes) == 2

        # First episode should have "API" and "async" keywords (higher score)
        assert "API" in episodes[0]["reflection"] or "async" in episodes[0]["reflection"]

        # All results should be from developer role
        for ep in episodes:
            assert ep["role"] == "developer"


# ============================================================================
# Test Suite 3: get_relevant_episodes with vector_memory ON
# ============================================================================


class TestGetRelevantEpisodesVectorMemoryOn:
    """Test that get_relevant_episodes with vector_memory ON boosts semantically similar episodes."""

    @pytest.fixture(autouse=True)
    def setup_db(self, test_db):
        """Set up test database with episodes and mock embeddings."""
        conn = get_db_connection(write=True)

        # Clear existing episodes
        conn.execute("DELETE FROM agent_episodes")

        # Episode 1: high semantic similarity (embedding close to query)
        ep1_embedding = json.dumps([0.9, 0.1, 0.1])
        conn.execute(
            """
            INSERT INTO agent_episodes (task_id, role, outcome, reflection, embedding, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "developer",
                "success",
                "Optimized database queries using connection pooling.",
                ep1_embedding,
                datetime.now().isoformat(),
            ),
        )

        # Episode 2: low semantic similarity (embedding far from query)
        ep2_embedding = json.dumps([0.1, 0.1, 0.9])
        conn.execute(
            """
            INSERT INTO agent_episodes (task_id, role, outcome, reflection, embedding, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                2,
                "developer",
                "success",
                "Updated UI components with new styling framework.",
                ep2_embedding,
                datetime.now().isoformat(),
            ),
        )

        conn.commit()
        conn.close()

    @mock.patch("equipa.embeddings.get_embedding")
    def test_vector_memory_boosts_similar_episodes(self, mock_get_embedding, test_db):
        """With vector_memory ON, semantically similar episodes should rank higher."""
        # Mock Ollama to return query embedding similar to episode 1
        query_embedding = [0.85, 0.15, 0.1]
        mock_get_embedding.return_value = query_embedding

        query = "Improve database performance"

        # Call get_relevant_episodes with vector_memory=True
        episodes = get_relevant_episodes(
            query, role_filter="developer", limit=2, vector_memory=True
        )

        # Should return 2 episodes
        assert len(episodes) == 2

        # Episode 1 (database/pooling) should rank higher due to semantic similarity
        assert "database" in episodes[0]["reflection"].lower()


# ============================================================================
# Test Suite 4: record_agent_episode embedding behavior
# ============================================================================


class TestRecordAgentEpisodeEmbedding:
    """Test that record_agent_episode handles embedding generation correctly."""

    @pytest.fixture(autouse=True)
    def setup_db(self, test_db):
        """Set up clean database."""
        conn = get_db_connection(write=True)
        conn.execute("DELETE FROM agent_episodes")
        conn.commit()
        conn.close()

    @mock.patch("equipa.embeddings.get_embedding")
    def test_embedding_called_on_success_with_vector_memory_on(
        self, mock_get_embedding, test_db
    ):
        """With vector_memory ON and outcome=success, embedding should be generated."""
        mock_get_embedding.return_value = [0.1, 0.2, 0.3]

        record_agent_episode(
            agent_run_id=1,
            role="developer",
            outcome="success",
            reflection="Fixed critical bug in authentication.",
            vector_memory=True,
        )

        # Verify embedding was called
        mock_get_embedding.assert_called_once()

        # Verify episode stored in database
        conn = get_db_connection(write=False)
        row = conn.execute(
            "SELECT embedding FROM agent_episodes WHERE reflection LIKE '%authentication%'"
        ).fetchone()
        assert row is not None
        assert row["embedding"] is not None

    @mock.patch("equipa.embeddings.get_embedding")
    def test_embedding_not_called_with_vector_memory_off(
        self, mock_get_embedding, test_db
    ):
        """With vector_memory OFF, embedding should not be generated."""
        record_agent_episode(
            agent_run_id=2,
            role="developer",
            outcome="success",
            reflection="Refactored code for clarity.",
            vector_memory=False,
        )

        # Embedding should not be called
        mock_get_embedding.assert_not_called()

        # Episode should still be stored
        conn = get_db_connection(write=False)
        row = conn.execute(
            "SELECT embedding FROM agent_episodes WHERE reflection LIKE '%Refactored%'"
        ).fetchone()
        assert row is not None
        assert row["embedding"] is None

    @mock.patch("equipa.embeddings.get_embedding")
    def test_embedding_failure_does_not_block_recording(
        self, mock_get_embedding, test_db
    ):
        """If embedding fails (Ollama down), episode should still be recorded."""
        mock_get_embedding.side_effect = Exception("Ollama service unavailable")

        # Should not raise exception
        record_agent_episode(
            agent_run_id=3,
            role="developer",
            outcome="success",
            reflection="Handled edge case in payment processing.",
            vector_memory=True,
        )

        # Episode should be stored without embedding
        conn = get_db_connection(write=False)
        row = conn.execute(
            "SELECT embedding FROM agent_episodes WHERE reflection LIKE '%payment%'"
        ).fetchone()
        assert row is not None
        assert row["embedding"] is None


# ============================================================================
# Test Suite 5: End-to-End Vector Memory Flow
# ============================================================================


class TestEndToEndVectorMemory:
    """Test the complete flow: insert episode with embedding, then retrieve with similar query."""

    @pytest.fixture(autouse=True)
    def setup_db(self, test_db):
        """Set up clean database."""
        conn = get_db_connection(write=True)
        conn.execute("DELETE FROM agent_episodes")
        conn.commit()
        conn.close()

    @mock.patch("equipa.embeddings.get_embedding")
    def test_insert_and_retrieve_similar_episode(self, mock_get_embedding, test_db):
        """Insert an episode with embedding, then retrieve it with a semantically similar query."""
        # Step 1: Record episode with embedding
        episode_reflection = "Optimized database connection pooling to reduce latency."
        episode_embedding = [0.8, 0.3, 0.1]

        def mock_embedding_side_effect(text):
            if "database connection pooling" in text:
                return episode_embedding
            # Query embedding similar to episode
            return [0.75, 0.35, 0.15]

        mock_get_embedding.side_effect = mock_embedding_side_effect

        record_agent_episode(
            agent_run_id=1,
            role="developer",
            outcome="success",
            reflection=episode_reflection,
            vector_memory=True,
        )

        # Step 2: Query with semantically similar text
        query = "How to improve database performance?"

        episodes = get_relevant_episodes(
            query, role_filter="developer", limit=1, vector_memory=True
        )

        # Should retrieve the inserted episode
        assert len(episodes) == 1
        assert "database" in episodes[0]["reflection"].lower()

    @mock.patch("equipa.embeddings.get_embedding")
    def test_dissimilar_query_ranks_lower(self, mock_get_embedding, test_db):
        """Insert two episodes, query should rank the more similar one higher."""
        # Insert episode 1: database-related
        ep1_embedding = [0.9, 0.1, 0.0]
        conn = get_db_connection(write=True)
        conn.execute(
            """
            INSERT INTO agent_episodes (task_id, role, outcome, reflection, embedding, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "developer",
                "success",
                "Optimized SQL queries for report generation.",
                json.dumps(ep1_embedding),
                datetime.now().isoformat(),
            ),
        )

        # Insert episode 2: UI-related
        ep2_embedding = [0.1, 0.9, 0.0]
        conn.execute(
            """
            INSERT INTO agent_episodes (task_id, role, outcome, reflection, embedding, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                2,
                "developer",
                "success",
                "Redesigned dashboard layout with responsive grid.",
                json.dumps(ep2_embedding),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        conn.close()

        # Query with embedding similar to episode 1
        query_embedding = [0.85, 0.15, 0.0]
        mock_get_embedding.return_value = query_embedding

        episodes = get_relevant_episodes(
            "Database performance tuning", role_filter="developer", limit=2, vector_memory=True
        )

        # Episode 1 (SQL/database) should rank first
        assert len(episodes) == 2
        assert "SQL" in episodes[0]["reflection"] or "database" in episodes[0]["reflection"].lower()


# ============================================================================
# Test Suite 6: Ollama HTTP Mocking
# ============================================================================


class TestOllamaMocking:
    """Test that urllib HTTP calls to Ollama are properly mocked."""

    @mock.patch("urllib.request.urlopen")
    def test_get_embedding_mocks_urllib(self, mock_urlopen):
        """Verify that get_embedding makes HTTP call via urllib."""
        # Mock Ollama response
        mock_response = mock.MagicMock()
        mock_response.read.return_value = json.dumps({"embedding": [0.1, 0.2, 0.3]}).encode()
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        embedding = get_embedding("test query")

        assert embedding == [0.1, 0.2, 0.3]
        mock_urlopen.assert_called_once()

    @mock.patch("urllib.request.urlopen")
    def test_get_embedding_handles_timeout(self, mock_urlopen):
        """If Ollama times out, get_embedding should return None."""
        import socket

        mock_urlopen.side_effect = socket.timeout("Request timed out")

        embedding = get_embedding("test query")

        assert embedding is None

    @mock.patch("urllib.request.urlopen")
    def test_get_embedding_handles_connection_error(self, mock_urlopen):
        """If Ollama is unreachable, get_embedding should return None."""
        import urllib.error

        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        embedding = get_embedding("test query")

        assert embedding is None


# ============================================================================
# Test Suite 7: find_similar_by_embedding
# ============================================================================


class TestFindSimilarByEmbedding:
    """Test find_similar_by_embedding function directly."""

    @pytest.fixture(autouse=True)
    def setup_db(self, test_db):
        """Set up database with multiple episodes with embeddings."""
        conn = get_db_connection(write=True)
        conn.execute("DELETE FROM agent_episodes")

        # Insert 3 episodes with known embeddings
        episodes = [
            (1, "developer", "success", "Fixed async bug", [0.9, 0.1, 0.0]),
            (2, "developer", "success", "Updated database schema", [0.1, 0.9, 0.0]),
            (3, "tester", "success", "Wrote integration tests", [0.0, 0.1, 0.9]),
        ]

        for task_id, role, outcome, reflection, embedding in episodes:
            conn.execute(
                """
                INSERT INTO agent_episodes (task_id, role, outcome, reflection, embedding, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    role,
                    outcome,
                    reflection,
                    json.dumps(embedding),
                    datetime.now().isoformat(),
                ),
            )

        conn.commit()
        conn.close()

    @mock.patch("equipa.embeddings.get_embedding")
    def test_find_similar_returns_sorted_by_similarity(self, mock_get_embedding, test_db):
        """find_similar_by_embedding should return episodes sorted by cosine similarity."""
        # Query embedding close to episode 1
        query_embedding = [0.85, 0.15, 0.0]
        mock_get_embedding.return_value = query_embedding

        results = find_similar_by_embedding(
            "Fix async issues", table="agent_episodes", limit=2
        )

        # Should return 2 results, sorted by similarity
        assert len(results) == 2

        # First result should be episode 1 (async bug)
        assert "async" in results[0]["reflection"].lower()

    @mock.patch("equipa.embeddings.get_embedding")
    def test_find_similar_returns_empty_on_ollama_failure(
        self, mock_get_embedding, test_db
    ):
        """If Ollama fails, find_similar_by_embedding should return empty list."""
        mock_get_embedding.return_value = None

        results = find_similar_by_embedding("query", table="agent_episodes", limit=5)

        assert results == []

    def test_find_similar_invalid_table_returns_empty(self, test_db):
        """If table name is invalid, find_similar_by_embedding should return empty list."""
        results = find_similar_by_embedding(
            "query", table="nonexistent_table", limit=5
        )

        assert results == []
