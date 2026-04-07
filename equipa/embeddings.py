"""EQUIPA embeddings module — Ollama vector embeddings with cosine similarity.

Layer 3 module: imports only from equipa.constants.
Uses urllib.request (stdlib) to call Ollama API, zero pip dependencies.
Default model: all-MiniLM-L6-v2 (384-dim).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from typing import Any

from equipa.constants import THEFORGE_DB


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two embedding vectors.

    Returns value in [-1, 1] where 1 = identical, 0 = orthogonal, -1 = opposite.
    Returns 0.0 if either vector is zero-length or inputs have mismatched dimensions.
    """
    if not a or not b or len(a) != len(b):
        return 0.0

    dot_product = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5

    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0

    return dot_product / (mag_a * mag_b)


def get_embedding(
    text: str,
    model: str = "all-MiniLM-L6-v2",
    base_url: str = "http://localhost:11434",
) -> list[float] | None:
    """Fetch embedding vector from Ollama API.

    Args:
        text: Input text to embed
        model: Ollama model name (default: all-MiniLM-L6-v2, 384-dim)
        base_url: Ollama server base URL

    Returns:
        Embedding vector as list[float], or None on failure (Ollama down, timeout, error)
    """
    if not text.strip():
        return None

    url = f"{base_url}/api/embeddings"
    payload = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data.get("embedding")
    except Exception:
        # Ollama down, timeout, or API error — return None gracefully
        return None


def embed_and_store_lesson(
    lesson_id: int,
    text: str,
    dispatch_config: dict[str, Any] | None = None,
) -> bool:
    """Generate embedding for a lesson and store as JSON in lessons.embedding column.

    If knowledge_graph feature is enabled, creates similarity edges to connect
    this lesson with other semantically similar lessons in the graph.

    Args:
        lesson_id: Lesson ID from lessons table
        text: Text to embed (typically lesson content)
        dispatch_config: Optional config with 'ollama_model' and 'ollama_base_url'

    Returns:
        True on success, False on Ollama failure or DB error
    """
    config = dispatch_config or {}
    model = config.get("ollama_model", "all-MiniLM-L6-v2")
    base_url = config.get("ollama_base_url", "http://localhost:11434")

    embedding = get_embedding(text, model=model, base_url=base_url)
    if embedding is None:
        return False

    try:
        from equipa.db import get_db_connection
        conn = get_db_connection(write=True)
        try:
            conn.execute(
                "UPDATE lessons_learned SET embedding = ? WHERE id = ?",
                (json.dumps(embedding), lesson_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        return False

    # Create similarity edges in knowledge graph (if enabled)
    knowledge_graph_enabled = False
    if dispatch_config:
        try:
            from equipa.dispatch import is_feature_enabled
            knowledge_graph_enabled = is_feature_enabled(dispatch_config, "knowledge_graph")
        except ImportError:
            pass

    if knowledge_graph_enabled and embedding:
        try:
            from equipa import graph
            # Create edges to similar lessons (threshold=0.8)
            edges_created = graph.create_similarity_edges(lesson_id, embedding, threshold=0.8)
            # Silent success — don't spam logs on every lesson storage
        except Exception:
            # Graph module unavailable or error — continue without graph updates
            pass

    return True


def embed_and_store_episode(
    episode_id: int,
    text: str,
    dispatch_config: dict[str, Any] | None = None,
) -> bool:
    """Generate embedding for an episode and store as JSON in episodes.embedding column.

    Args:
        episode_id: Episode ID from episodes table
        text: Text to embed (typically episode outcome + lesson)
        dispatch_config: Optional config with 'ollama_model' and 'ollama_base_url'

    Returns:
        True on success, False on Ollama failure or DB error
    """
    config = dispatch_config or {}
    model = config.get("ollama_model", "all-MiniLM-L6-v2")
    base_url = config.get("ollama_base_url", "http://localhost:11434")

    embedding = get_embedding(text, model=model, base_url=base_url)
    if embedding is None:
        return False

    try:
        from equipa.db import get_db_connection
        conn = get_db_connection(write=True)
        try:
            conn.execute(
                "UPDATE agent_episodes SET embedding = ? WHERE id = ?",
                (json.dumps(embedding), episode_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def find_similar_by_embedding(
    query_text: str,
    table: str,
    top_k: int = 5,
    dispatch_config: dict[str, Any] | None = None,
) -> list[tuple[int, float]]:
    """Find most similar lessons or episodes using brute-force cosine similarity.

    Args:
        query_text: Query text to embed and compare
        table: Table name ('lessons' or 'episodes')
        top_k: Number of results to return
        dispatch_config: Optional config with 'ollama_model' and 'ollama_base_url'

    Returns:
        List of (row_id, similarity_score) tuples sorted by descending similarity.
        Returns empty list on Ollama failure, DB error, or if table has no embeddings.
    """
    if table not in {"lessons", "episodes"}:
        return []

    # Map logical table names to actual DB table names
    table_map = {
        "lessons": "lessons_learned",
        "episodes": "agent_episodes",
    }
    actual_table = table_map[table]

    config = dispatch_config or {}
    model = config.get("ollama_model", "all-MiniLM-L6-v2")
    base_url = config.get("ollama_base_url", "http://localhost:11434")

    query_embedding = get_embedding(query_text, model=model, base_url=base_url)
    if query_embedding is None:
        return []

    try:
        conn = sqlite3.connect(str(THEFORGE_DB))
        try:
            cursor = conn.execute(f"SELECT id, embedding FROM {actual_table} WHERE embedding IS NOT NULL")
            rows = cursor.fetchall()

            scores: list[tuple[int, float]] = []
            for row_id, embedding_json in rows:
                try:
                    embedding = json.loads(embedding_json)
                    score = cosine_similarity(query_embedding, embedding)
                    scores.append((row_id, score))
                except (json.JSONDecodeError, TypeError):
                    continue

            # Sort by descending similarity, take top_k
            scores.sort(key=lambda x: x[1], reverse=True)
            return scores[:top_k]
        finally:
            conn.close()
    except Exception:
        return []
