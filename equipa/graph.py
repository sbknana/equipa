"""EQUIPA knowledge graph — PageRank, community detection, and edge management.

Layer 4: Imports from equipa.db and equipa.constants only.
Pure Python PageRank implementation via power iteration.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import math
import random
from collections import defaultdict

from equipa.db import get_db_connection


# --- Edge Management ---

def add_edge(
    src_id: int,
    dst_id: int,
    edge_type: str,
    weight: float = 1.0,
) -> None:
    """Add or update a directed edge between two lessons in the knowledge graph.

    Args:
        src_id: Source lesson ID
        dst_id: Destination lesson ID
        edge_type: Edge type (e.g., "coaccessed", "similarity", "sequence")
        weight: Edge weight (default 1.0)

    Uses REPLACE to handle duplicate edges (updates weight if edge exists).
    """
    conn = get_db_connection(write=True)
    conn.execute(
        """INSERT OR REPLACE INTO lesson_graph_edges
           (src_id, dst_id, edge_type, weight)
           VALUES (?, ?, ?, ?)""",
        (src_id, dst_id, edge_type, weight),
    )
    conn.commit()
    conn.close()


def get_adjacency_list() -> dict[int, list[tuple[int, float]]]:
    """Build adjacency list from lesson_graph_edges table.

    Returns:
        Dictionary mapping node_id -> [(neighbor_id, weight), ...]
    """
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT src_id, dst_id, weight FROM lesson_graph_edges"
    ).fetchall()
    conn.close()

    adj: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for row in rows:
        adj[row["src_id"]].append((row["dst_id"], row["weight"]))
    return dict(adj)


def create_coaccessed_edges(lesson_ids: list[int]) -> int:
    """Create edges between all pairs of lessons that were co-accessed.

    Args:
        lesson_ids: List of lesson IDs that were accessed together

    Returns:
        Number of edges created
    """
    if len(lesson_ids) < 2:
        return 0

    conn = get_db_connection(write=True)
    created = 0
    for i, src in enumerate(lesson_ids):
        for dst in lesson_ids[i + 1:]:
            # Bidirectional edges
            conn.execute(
                """INSERT OR IGNORE INTO lesson_graph_edges
                   (src_id, dst_id, edge_type, weight)
                   VALUES (?, ?, 'coaccessed', 1.0)""",
                (src, dst),
            )
            conn.execute(
                """INSERT OR IGNORE INTO lesson_graph_edges
                   (src_id, dst_id, edge_type, weight)
                   VALUES (?, ?, 'coaccessed', 1.0)""",
                (dst, src),
            )
            created += 2
    conn.commit()
    conn.close()
    return created


def create_similarity_edges(
    lesson_id: int,
    embedding: list[float],
    threshold: float = 0.8,
) -> int:
    """Find similar lessons via cosine similarity and create edges.

    Args:
        lesson_id: Target lesson ID
        embedding: Embedding vector for the lesson
        threshold: Minimum cosine similarity to create edge (default 0.8)

    Returns:
        Number of edges created
    """
    import json

    conn = get_db_connection()
    rows = conn.execute(
        "SELECT id, embedding FROM lessons_learned WHERE id != ? AND embedding IS NOT NULL",
        (lesson_id,),
    ).fetchall()
    conn.close()

    created = 0
    for row in rows:
        try:
            other_emb = json.loads(row["embedding"])
            similarity = _cosine_similarity(embedding, other_emb)
            if similarity >= threshold:
                add_edge(lesson_id, row["id"], "similarity", similarity)
                created += 1
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return created


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Args:
        a: First vector
        b: Second vector

    Returns:
        Cosine similarity in [0, 1] (assumes non-negative embeddings)
    """
    if len(a) != len(b):
        return 0.0

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# --- PageRank ---

def pagerank(
    adj: dict[int, list[tuple[int, float]]],
    damping: float = 0.85,
    iterations: int = 30,
    tolerance: float = 1e-6,
) -> dict[int, float]:
    """Compute PageRank scores via power iteration.

    Args:
        adj: Adjacency list {node -> [(neighbor, weight), ...]}
        damping: Damping factor (default 0.85)
        iterations: Maximum iterations (default 30)
        tolerance: Convergence tolerance (default 1e-6)

    Returns:
        Dictionary mapping node_id -> PageRank score
    """
    # Collect all nodes (source and destination)
    nodes = set(adj.keys())
    for neighbors in adj.values():
        for neighbor, _ in neighbors:
            nodes.add(neighbor)
    nodes = list(nodes)

    if not nodes:
        return {}

    n = len(nodes)
    node_to_idx = {node: i for i, node in enumerate(nodes)}

    # Initialize uniform distribution
    scores = [1.0 / n] * n
    new_scores = [0.0] * n

    # Compute out-degree for each node (sum of edge weights)
    out_degree = [0.0] * n
    for node, neighbors in adj.items():
        idx = node_to_idx[node]
        out_degree[idx] = sum(weight for _, weight in neighbors)

    # Power iteration
    for _ in range(iterations):
        # Reset new_scores
        new_scores = [(1.0 - damping) / n] * n

        # Distribute rank from each node to its neighbors
        for node, neighbors in adj.items():
            src_idx = node_to_idx[node]
            if out_degree[src_idx] == 0:
                # Dangling node: redistribute evenly to all nodes
                contribution = damping * scores[src_idx] / n
                for i in range(n):
                    new_scores[i] += contribution
            else:
                # Regular node: distribute proportionally to edge weights
                for neighbor, weight in neighbors:
                    dst_idx = node_to_idx[neighbor]
                    contribution = damping * scores[src_idx] * weight / out_degree[src_idx]
                    new_scores[dst_idx] += contribution

        # Check convergence
        diff = sum(abs(new_scores[i] - scores[i]) for i in range(n))
        scores = new_scores[:]
        if diff < tolerance:
            break

    # Return as dict
    return {nodes[i]: scores[i] for i in range(n)}


# --- Label Propagation ---

def label_propagation(
    adj: dict[int, list[tuple[int, float]]],
    max_iterations: int = 20,
) -> dict[int, int]:
    """Detect communities via label propagation algorithm.

    Each node adopts the most common label among its neighbors.
    Randomized for tie-breaking.

    Args:
        adj: Adjacency list {node -> [(neighbor, weight), ...]}
        max_iterations: Maximum iterations (default 20)

    Returns:
        Dictionary mapping node_id -> community_id
    """
    # Collect all nodes
    nodes = set(adj.keys())
    for neighbors in adj.values():
        for neighbor, _ in neighbors:
            nodes.add(neighbor)
    nodes = list(nodes)

    if not nodes:
        return {}

    # Initialize each node with its own community
    labels = {node: node for node in nodes}

    for _ in range(max_iterations):
        # Shuffle nodes for random update order
        shuffled = nodes[:]
        random.shuffle(shuffled)

        changed = False
        for node in shuffled:
            # Count weighted votes from neighbors
            votes: dict[int, float] = defaultdict(float)
            for neighbor, weight in adj.get(node, []):
                votes[labels[neighbor]] += weight

            if not votes:
                continue

            # Find most common label (random tie-break)
            max_vote = max(votes.values())
            candidates = [label for label, vote in votes.items() if vote == max_vote]
            new_label = random.choice(candidates)

            if new_label != labels[node]:
                labels[node] = new_label
                changed = True

        if not changed:
            break

    return labels


# --- Graph-Enhanced Ranking ---

def rerank_with_graph(
    candidates: list[dict],
    pr_scores: dict[int, float],
    sim_weight: float = 0.7,
    graph_weight: float = 0.3,
) -> list[dict]:
    """Rerank lesson candidates by blending similarity and PageRank scores.

    Args:
        candidates: List of dicts with 'id' and 'similarity' keys
        pr_scores: PageRank scores from pagerank()
        sim_weight: Weight for similarity score (default 0.7)
        graph_weight: Weight for PageRank score (default 0.3)

    Returns:
        Reranked list of candidates (same dicts, sorted by combined score)
    """
    # Normalize PageRank scores to [0, 1]
    if pr_scores:
        max_pr = max(pr_scores.values())
        norm_pr = {k: v / max_pr for k, v in pr_scores.items()} if max_pr > 0 else pr_scores
    else:
        norm_pr = {}

    # Compute combined scores
    for candidate in candidates:
        lesson_id = candidate["id"]
        sim_score = candidate.get("similarity", 0.0)
        pr_score = norm_pr.get(lesson_id, 0.0)
        candidate["combined_score"] = sim_weight * sim_score + graph_weight * pr_score

    # Sort by combined score descending
    candidates.sort(key=lambda c: c.get("combined_score", 0.0), reverse=True)
    return candidates
