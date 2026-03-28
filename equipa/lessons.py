"""EQUIPA lessons module — lesson formatting, episode recording, and q-value updates.

Layer 3: Imports from equipa.constants, equipa.db, equipa.parsing.
Uses late imports for sanitizer functions (from lesson_sanitizer) and
monolith functions (wrap_untrusted, _make_untrusted_delimiter) until they
are extracted.

Extracted from forge_orchestrator.py as part of Phase 3 monolith split.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from equipa.constants import THEFORGE_DB
from equipa.db import ensure_schema, get_db_connection
from equipa.parsing import (
    compute_initial_q_value,
    compute_keyword_overlap,
    parse_approach_summary,
    parse_error_patterns,
    parse_reflection,
)


# --- Lesson Formatting ---

def format_lessons_for_injection(
    lessons: list[dict],
    delimiter: str | None = None,
) -> str:
    """Format lessons_learned for injection into agent prompts.

    Sanitizes each lesson's content before formatting to prevent prompt
    injection (PM-24, PM-28). Wraps the entire output in <task-input> tags
    AND unpredictable delimiter markers so agents treat lesson content as
    data, not instructions.

    Args:
        lessons: List of lesson dicts from get_relevant_lessons
        delimiter: Unpredictable boundary token (from _make_untrusted_delimiter)

    Returns:
        Formatted string with lessons wrapped in isolation markers.
    """
    if not lessons:
        return ""

    # Late imports for sanitizer (may not be available)
    from equipa.security import wrap_untrusted
    try:
        from lesson_sanitizer import (
            sanitize_error_signature,
            sanitize_lesson_content,
            wrap_lessons_in_task_input,
        )
    except ImportError:
        # Fallback stubs — no sanitization if module unavailable
        def sanitize_lesson_content(text):
            return text or ""
        def sanitize_error_signature(sig):
            return sig or ""
        def wrap_lessons_in_task_input(text):
            return text or ""

    lines = []
    for lesson in lessons:
        # Sanitize lesson text before injecting into prompt
        safe_lesson = sanitize_lesson_content(lesson['lesson'])
        if not safe_lesson:
            continue
        lines.append(f"- {safe_lesson}")
        # Sanitize error signature context too
        if lesson.get('error_signature'):
            safe_sig = sanitize_error_signature(lesson['error_signature'])
            lines.append(f"  (Error: {safe_sig}, seen {lesson['times_seen']}x)")

    formatted = "\n".join(lines)

    # Wrap in task-input tags so agents treat this as data (PM-24)
    wrapped = wrap_lessons_in_task_input(formatted)
    if not wrapped:
        return ""

    # Always prepend section heading
    header = "## Lessons from Previous Runs\n"

    # Add unpredictable delimiter for defense-in-depth (EQ-24)
    if delimiter:
        wrapped = (
            f'{header}<task-input type="lessons" trust="derived">\n'
            f'{wrap_untrusted(wrapped, delimiter)}\n</task-input>'
        )
    else:
        wrapped = f'{header}{wrapped}'

    return wrapped


def update_lesson_injection_count(lesson_ids: list[int]) -> None:
    """Increment times_injected counter for the given lesson IDs.

    Args:
        lesson_ids: List of lesson IDs to update
    """
    if not lesson_ids:
        return

    try:
        conn = sqlite3.connect(str(THEFORGE_DB))
        placeholders = ",".join("?" * len(lesson_ids))
        conn.execute(
            f"UPDATE lessons_learned SET times_injected = times_injected + 1 "
            f"WHERE id IN ({placeholders})",
            lesson_ids
        )
        conn.commit()
        conn.close()
    except Exception as e:
        # Don't fail the orchestrator if lesson update fails
        print(f"Warning: Failed to update lesson injection count: {e}")


# --- Episode Injection (MemRL pattern) ---

# Track which episode IDs were injected per task_id, so we can update q_values
# after the task completes. Keyed by task_id, value is list of episode IDs.
_injected_episodes_by_task: dict[int, list[int]] = {}


def get_active_simba_rules():
    """Load active SIMBA-synthesized rules from lessons_learned."""
    try:
        conn = get_db_connection()
        rows = conn.execute(
            """SELECT lesson, error_signature FROM lessons_learned
               WHERE active = 1 AND (source = 'simba' OR source = 'forgesmith')
               AND lesson IS NOT NULL AND lesson != ''"""
        ).fetchall()
        conn.close()
        return [{"lesson": r["lesson"], "signature": r["error_signature"]} for r in rows]
    except Exception:
        return []


def get_relevant_episodes(
    role: str,
    project_id: int,
    task_type: str | None = None,
    min_q_value: float = 0.3,
    limit: int = 3,
    task_description: str | None = None,
    dispatch_config: dict | None = None,
) -> list[dict]:
    """Fetch relevant past episodes for injection into agent prompts.

    Matches by: same role + same project + optionally similar task_type.
    Filters by q_value > min_q_value (only inject useful experiences).

    Scoring combines:
    - q_value (base quality signal)
    - Task description keyword overlap (if task_description provided)
    - Recency weighting (episodes from last 7 days weighted 2x)
    - Vector similarity (if vector_memory feature enabled and Ollama available)

    Args:
        role: Agent role (e.g. 'developer', 'tester')
        project_id: Project ID to match episodes from
        task_type: Optional task type for similarity matching
        min_q_value: Minimum q_value threshold (default 0.3)
        limit: Maximum episodes to return (default 3)
        task_description: Optional task description for keyword similarity scoring
        dispatch_config: Optional config dict for feature flags and Ollama settings

    Returns:
        List of episode dicts with id, approach_summary, outcome, reflection, q_value
    """
    try:
        # Load SIMBA rules once for scoring
        _simba_rules = get_active_simba_rules()

        conn = get_db_connection()
        # Fetch more candidates than needed so we can re-rank
        fetch_limit = max(limit * 3, 10)

        # Primary match: same role + same project + q_value above threshold + has reflection
        rows = conn.execute(
            """SELECT id, task_id, task_type, project_id, approach_summary, outcome,
                      reflection, q_value, turns_used, created_at
               FROM agent_episodes
               WHERE role = ? AND project_id = ? AND q_value > ?
                 AND reflection IS NOT NULL AND reflection != ''
               ORDER BY q_value DESC, created_at DESC
               LIMIT ?""",
            (role, project_id, min_q_value, fetch_limit),
        ).fetchall()

        episodes = [dict(r) for r in rows]

        # If we got fewer than limit, try matching by role + task_type across projects
        if len(episodes) < limit and task_type:
            existing_ids = {e["id"] for e in episodes}
            remaining = limit - len(episodes)
            cross_rows = conn.execute(
                """SELECT id, task_id, task_type, project_id, approach_summary, outcome,
                          reflection, q_value, turns_used, created_at
                   FROM agent_episodes
                   WHERE role = ? AND task_type = ? AND q_value > ?
                     AND reflection IS NOT NULL AND reflection != ''
                   ORDER BY q_value DESC, created_at DESC
                   LIMIT ?""",
                (role, task_type, min_q_value, remaining + len(existing_ids)),
            ).fetchall()
            for r in cross_rows:
                if dict(r)["id"] not in existing_ids:
                    episodes.append(dict(r))
                    existing_ids.add(dict(r)["id"])

        conn.close()

        # Score and re-rank episodes
        now = datetime.now()
        seven_days_ago = now - timedelta(days=7)

        # Vector similarity scoring (if enabled)
        vector_scores: dict[int, float] = {}
        vector_memory_enabled = False
        if dispatch_config:
            try:
                from equipa.dispatch import is_feature_enabled
                vector_memory_enabled = is_feature_enabled(dispatch_config, "vector_memory")
            except ImportError:
                pass

        if vector_memory_enabled and task_description:
            try:
                from equipa.embeddings import find_similar_by_embedding, cosine_similarity
                # Find similar episodes by embedding
                similar_episodes = find_similar_by_embedding(
                    task_description, "episodes", top_k=20, dispatch_config=dispatch_config
                )
                for ep_id, sim_score in similar_episodes:
                    vector_scores[ep_id] = sim_score
            except Exception:
                # Ollama down or import failed — gracefully continue without vector scoring
                pass

        for ep in episodes:
            score = ep.get("q_value", 0.5)

            # Recency weighting: episodes from last 7 days get 2x weight
            created = ep.get("created_at")
            if created:
                try:
                    ep_date = datetime.fromisoformat(
                        created.replace("Z", "+00:00").split("+")[0]
                    )
                    if ep_date >= seven_days_ago:
                        score *= 2.0
                except (ValueError, AttributeError):
                    pass  # Can't parse date, skip recency bonus

            # Task description keyword overlap scoring
            keyword_score = 0.0
            if task_description:
                ep_text = (
                    (ep.get("approach_summary", "") or "")
                    + " "
                    + (ep.get("reflection", "") or "")
                )
                overlap = compute_keyword_overlap(task_description, ep_text)
                keyword_score = overlap
                # Boost score by up to 50% based on keyword overlap
                score *= (1.0 + overlap * 0.5)

            # Vector similarity scoring (blended with keyword scoring)
            ep_id = ep.get("id")
            if ep_id in vector_scores:
                cosine_sim = vector_scores[ep_id]
                # Blend: 60% existing score (keyword + q_value) + 40% vector similarity
                score = 0.6 * score + 0.4 * cosine_sim

            # SIMBA rule bonus: boost episodes that followed synthesized rules
            if _simba_rules:
                ep_text_lower = (
                    (ep.get("approach_summary", "") or "")
                    + " "
                    + (ep.get("reflection", "") or "")
                ).lower()
                for rule in _simba_rules:
                    # Check if episode mentions following this rule's key terms
                    rule_keywords = set(rule["lesson"].lower().split())
                    ep_keywords = set(ep_text_lower.split())
                    keyword_match = len(rule_keywords & ep_keywords) / max(len(rule_keywords), 1)
                    if keyword_match > 0.3:
                        positive = ep.get("outcome", "") in ("tests_passed", "no_tests")
                        if positive:
                            score += 0.15  # Boost episodes that followed rules and succeeded
                        break  # One rule match is enough

            ep["_relevance_score"] = score

        # Sort by relevance score descending
        episodes.sort(key=lambda e: e.get("_relevance_score", 0), reverse=True)

        # Return top N, strip internal scoring field
        result = episodes[:limit]
        for ep in result:
            ep.pop("_relevance_score", None)
            ep.pop("created_at", None)

        return result
    except Exception as e:
        print(f"Warning: Failed to fetch relevant episodes: {e}")
        return []


def format_episodes_for_injection(
    episodes: list[dict],
    delimiter: str | None = None,
) -> str:
    """Format agent episodes for injection into agent prompts.

    Args:
        episodes: List of episode dicts from get_relevant_episodes
        delimiter: Unpredictable boundary token (from _make_untrusted_delimiter)

    Returns:
        Formatted string under "## Past Experience" heading (2-3 sentences each),
        wrapped in untrusted content markers when delimiter is provided.
    """
    if not episodes:
        return ""

    from equipa.security import wrap_untrusted

    lines = ["## Past Experience", ""]
    for ep in episodes:
        summary = ep.get("approach_summary") or "No summary"
        outcome = ep.get("outcome", "unknown")
        reflection = ep.get("reflection") or "No lesson recorded"
        # Truncate to keep injected text short
        if len(summary) > 120:
            summary = summary[:117] + "..."
        if len(reflection) > 150:
            reflection = reflection[:147] + "..."
        lines.append(
            f"- Previous similar task: {summary}. "
            f"Outcome: {outcome}. Lesson: {reflection}"
        )

    formatted = "\n".join(lines)

    # Wrap in unpredictable delimiter for defense-in-depth (EQ-24)
    if delimiter:
        formatted = (
            f"## Past Experience\n\n"
            f'<task-input type="episodes" trust="derived">\n'
            f"{wrap_untrusted(chr(10).join(lines[2:]), delimiter)}\n"
            f"</task-input>"
        )

    return formatted


def record_agent_episode(
    task: dict | int,
    result: dict | None,
    outcome: str,
    role: str = "developer",
    output: list | None = None,
    dispatch_config: dict | None = None,
) -> None:
    """Store a Reflexion episode in the agent_episodes table.

    Extracts reflection from agent output. If no reflection found in
    the output, records the episode with a null reflection (the
    orchestrator will attempt a standalone reflexion call separately).

    After INSERT, attempts to generate and store embedding if vector_memory
    feature is enabled. Embedding generation failure does not block episode
    recording.

    Never crashes the orchestrator — all errors are logged and swallowed.
    """
    try:
        from equipa.output import log

        ensure_schema()

        task_id = task.get("id") if isinstance(task, dict) else task
        project_id = task.get("project_id") if isinstance(task, dict) else None
        task_type = (
            task.get("task_type") or task.get("role") or role
            if isinstance(task, dict)
            else role
        )
        result_text = result.get("result_text", "") if isinstance(result, dict) else ""
        num_turns = result.get("num_turns", 0) if isinstance(result, dict) else 0

        reflection = parse_reflection(result_text)
        approach = parse_approach_summary(result_text)
        error_patterns = parse_error_patterns(result, outcome=outcome, result_text=result_text)
        q_value = compute_initial_q_value(outcome)

        conn = get_db_connection(write=True)
        cursor = conn.execute(
            """INSERT INTO agent_episodes
               (task_id, role, task_type, project_id, approach_summary,
                turns_used, outcome, error_patterns, reflection, q_value)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, role, task_type, project_id, approach,
             num_turns, outcome, error_patterns, reflection, q_value),
        )
        episode_id = cursor.lastrowid
        conn.commit()
        conn.close()

        # Attempt to generate and store embedding (if vector_memory enabled)
        vector_memory_enabled = False
        if dispatch_config:
            try:
                from equipa.dispatch import is_feature_enabled
                vector_memory_enabled = is_feature_enabled(dispatch_config, "vector_memory")
            except ImportError:
                pass

        if vector_memory_enabled and episode_id:
            try:
                from equipa.embeddings import embed_and_store_episode
                # Combine approach + reflection for embedding
                ep_text = f"{approach or ''} {reflection or ''}".strip()
                if ep_text:
                    success = embed_and_store_episode(episode_id, ep_text, dispatch_config)
                    if not success:
                        # Ollama down or error — log but don't fail
                        log("  [VectorMemory] Failed to generate episode embedding (Ollama down?)", output)
            except Exception:
                # Import failed or unexpected error — continue without embedding
                pass

        # Log failure classification if present
        failure_class_info = ""
        if error_patterns:
            try:
                parsed = json.loads(error_patterns)
                fc = parsed.get("failure_class", "unknown")
                conf = parsed.get("confidence", "?")
                secondary = parsed.get("secondary_classes", [])
                sec_str = f" +{','.join(secondary)}" if secondary else ""
                failure_class_info = f" [class={fc}{sec_str}, conf={conf}]"
            except (json.JSONDecodeError, TypeError):
                pass  # legacy plain-text format

        if reflection:
            # Truncate for log display
            preview = reflection[:120] + "..." if len(reflection) > 120 else reflection
            log(f"  [Reflexion] Recorded episode{failure_class_info} with reflection: {preview}", output)
        else:
            log(f"  [Reflexion] Recorded episode{failure_class_info} (no reflection in output)", output)

    except Exception as e:
        print(f"  [Reflexion] WARNING: Failed to record episode: {e}")


# --- Episode Injection Count & Q-Value Updates ---

def update_episode_injection_count(episode_ids: list[int]) -> None:
    """Increment times_injected counter for the given episode IDs.

    Args:
        episode_ids: List of episode IDs that were injected into a prompt
    """
    if not episode_ids:
        return

    try:
        ensure_schema()
        conn = get_db_connection(write=True)
        placeholders = ",".join("?" * len(episode_ids))
        conn.execute(
            f"UPDATE agent_episodes SET times_injected = times_injected + 1 "
            f"WHERE id IN ({placeholders})",
            episode_ids
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Warning: Failed to update episode injection count: {e}")


def update_episode_q_values(
    injected_episode_ids: list[int],
    task_succeeded: bool,
) -> None:
    """Update q_values of previously injected episodes based on task outcome.

    Implements the MemRL reward signal:
    - If task succeeded and injected episode was useful: q_value += 0.1
    - If task failed despite injected episode: q_value -= 0.05
    - Q-values are bounded to [0.0, 1.0]

    Args:
        injected_episode_ids: List of episode IDs that were injected before this task
        task_succeeded: Whether the task completed successfully
    """
    if not injected_episode_ids:
        return

    try:
        conn = get_db_connection(write=True)
        if task_succeeded:
            delta = 0.1
        else:
            delta = -0.05

        for ep_id in injected_episode_ids:
            # Bounded update: clamp to [0.0, 1.0]
            conn.execute(
                """UPDATE agent_episodes
                   SET q_value = MIN(1.0, MAX(0.0, q_value + ?))
                   WHERE id = ?""",
                (delta, ep_id),
            )

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Warning: Failed to update episode q_values: {e}")


def update_injected_episode_q_values_for_task(
    task_id: int,
    outcome: str,
    output: list | None = None,
) -> None:
    """Look up which episodes were injected for a task and update their q_values.

    Called after task completion. Uses the _injected_episodes_by_task tracker
    to find which episodes were injected, then applies the MemRL reward signal.

    Args:
        task_id: The task ID that just completed
        outcome: The task outcome string (e.g. 'tests_passed', 'developer_failed')
        output: Optional output buffer for logging
    """
    from equipa.output import log

    ep_ids = _injected_episodes_by_task.pop(task_id, [])
    if not ep_ids:
        return

    task_succeeded = outcome in ("tests_passed", "no_tests")
    update_episode_q_values(ep_ids, task_succeeded)

    delta = "+0.1" if task_succeeded else "-0.05"
    log(f"  [MemRL] Updated q_values ({delta}) for {len(ep_ids)} injected episodes: {ep_ids}", output)
