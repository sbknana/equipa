# GEPA Episode History Check — Task #1603

## Summary

Added validation that checks GEPA prompt mutation candidates against failed episode
history before accepting them. When a proposed mutation resembles a previously failed
approach, the candidate's fitness score is penalized (multiplied by 0.7) rather than
blocked — allowing it to still succeed in different contexts while discouraging
repetition of known failures.

## Implementation

### New Functions in `forgesmith_gepa.py`

| Function | Purpose |
|---|---|
| `compute_keyword_overlap(text_a, text_b)` | Jaccard similarity on word sets (0.0–1.0) |
| `get_failed_episodes_by_keywords(role, q_value_threshold, lookback_days)` | Fetch failed episodes with low q_values for a role |
| `check_episode_history_for_candidate(role, mutation_diff, ...)` | Compare mutation diff against failed episode approach_summaries |
| `extract_mutation_diff(old_text, new_text)` | Extract added lines from unified diff between old and new prompt |

### Constants

| Constant | Value | Purpose |
|---|---|---|
| `EPISODE_HISTORY_OVERLAP_THRESHOLD` | 0.4 | Minimum Jaccard overlap to consider a mutation similar to a failed episode |
| `EPISODE_HISTORY_PENALTY` | 0.7 | Fitness multiplier when matching failed episodes found |

### Pipeline Integration

The check is wired into `run_gepa_for_role()` after GEPA produces a candidate prompt
and passes safety validation. The flow:

1. GEPA optimizer produces `evolved_prompt`
2. `validate_evolved_prompt()` checks diff ratio, protected sections, length
3. **NEW:** `extract_mutation_diff()` gets the behavioral change (added lines)
4. **NEW:** `check_episode_history_for_candidate()` compares against failed episodes
5. Result includes `episode_history_check` dict with penalty info
6. `store_evolved_prompt()` records penalty data in `forgesmith_changes.evidence` JSON

### Episode Query

`get_failed_episodes_by_keywords()` queries `agent_episodes` for:
- Role matches the candidate's role
- `q_value < 0.3` (low-performing episodes)
- `outcome` is one of: `early_terminated`, `blocked`, `cycles_exhausted`, `developer_max_turns`
- Created within the last 90 days
- Has a non-empty `approach_summary`
- Limited to 200 episodes (sorted by lowest q_value first)

### Penalty Logic

For each failed episode, the mutation diff text is compared against
`approach_summary + " " + reflection` using Jaccard keyword overlap. If any
episode exceeds the 0.4 threshold, the candidate is penalized:

- **Penalized:** fitness score multiplied by 0.7
- **NOT blocked:** candidate still proceeds (might work in different context)
- **Logged:** warning with match count and max overlap score
- **Recorded:** in `forgesmith_changes.evidence` JSON for audit

## Files Changed

| File | Change |
|---|---|
| `forgesmith_gepa.py` | Added 4 functions, 2 constants, wired into evaluation pipeline |
| `tests/test_gepa_episode_check.py` | 26 pytest tests (all passing) |
| `tests/conftest.py` | Fixed `pytest_configure` to handle missing `equipa.cli` gracefully |

## Tests

26 tests across 5 test classes:

- **TestComputeKeywordOverlap** (9): identical, different, partial, empty, None, case, punctuation
- **TestExtractMutationDiff** (5): added lines, removed lines, identical, empty, multiline
- **TestGetFailedEpisodesByKeywords** (3): filter by role, filter by outcome/q_value, empty DB
- **TestCheckEpisodeHistoryForCandidate** (7): penalize matching, no penalty novel, empty episodes, empty/None mutation, multiple matches, custom threshold/penalty
- **TestIntegrationEpisodeCheck** (1): full structure validation

All 26 tests pass in 3.72s.

## Design Decisions

1. **Inline `compute_keyword_overlap` rather than importing from `equipa.parsing`**: The
   worktree may not have the full `equipa` package (monolith split in progress). Local copy
   ensures `forgesmith_gepa.py` remains self-contained.

2. **Penalty not block**: Task description explicitly says "do NOT block the candidate entirely
   (it might work in a different context)". A 0.7x penalty degrades fitness without eliminating.

3. **90-day lookback for failed episodes**: Wider than the 60-day GEPA training window to catch
   older failures that may still be relevant.

4. **DB connection in finally block**: `get_failed_episodes_by_keywords` uses try/finally to
   ensure connection closure, addressing the recurring QS-01 pattern.
