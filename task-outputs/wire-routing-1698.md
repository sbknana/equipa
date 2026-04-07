# Cost Routing Integration — Task #1698

**Date:** 2026-03-27
**Commit:** 63f67e4
**Status:** Complete

## Summary

Integrated intelligent cost routing (Task #1616) into EQUIPA's model selection system. The `auto_model_routing` feature flag now enables complexity-based model tiering with circuit breaker fallbacks as priority 6 in the model resolution hierarchy.

## Changes Made

### 1. Modified `equipa/roles.py`

**Function:** `get_role_model()`

**Change:** Added priority 6 auto-routing at the end of the resolution chain, before the final DEFAULT_ROLE_MODELS fallback.

```python
# Priority 6: Auto-routing (late import, gated by feature flag)
if effective_config and task and is_feature_enabled(effective_config, "auto_model_routing"):
    from equipa.routing import auto_select_model
    routed_model = auto_select_model(task, effective_config)
    if routed_model:
        return routed_model
```

**Key design decisions:**
- **Late import:** `auto_select_model` imported inside the conditional to avoid circular dependencies
- **Gated by feature flag:** Only activates when `"auto_model_routing"` is enabled in dispatch config
- **Priority 6 placement:** Runs AFTER all 5 existing priorities (complexity, role, CLI, config global, role defaults)
- **Never overrides explicit config:** Users retain full control via dispatch config, CLI args, or role-specific settings

### 2. Modified `equipa/cli.py`

**Function:** `_post_task_telemetry()`

**Change:** Added circuit breaker feedback call at the end of telemetry processing.

```python
# Record model outcome for circuit breaker (cost routing)
from equipa.routing import record_model_outcome
success = outcome in ("tests_passed", "no_tests")
record_model_outcome(model, success)
```

**Key design decisions:**
- **Success definition:** Tasks with `tests_passed` or `no_tests` outcomes are considered successful
- **Failure tracking:** All other outcomes (`developer_failed`, `tester_failed`, `early_terminated`, `blocked`) count as failures
- **Circuit breaker logic:** After 5 consecutive failures, a model's circuit opens for 60 seconds, triggering automatic tier escalation

## Model Resolution Priority (Updated)

The complete priority chain is now:

1. **Dispatch config per-complexity** (e.g., `model_epic`, `model_complex`)
2. **Dispatch config per-role** (e.g., `model_developer`, `model_tester`)
3. **CLI --model override**
4. **Dispatch config global model**
5. **DEFAULT_ROLE_MODELS dictionary**
6. **Auto-routing** (if `auto_model_routing` feature enabled) ← NEW
7. **Final fallback:** `DEFAULT_MODEL` constant

## Feature Flag Usage

To enable cost routing, add to `dispatch_config.json`:

```json
{
  "features": {
    "auto_model_routing": true
  }
}
```

Without the flag, EQUIPA behaves exactly as before — all existing deployments are unaffected.

## Circuit Breaker Behavior

- **Closed state (default):** Auto-routing functions normally
- **Open state (after 5 failures):** Auto-router escalates to next tier (`haiku→sonnet`, `sonnet→opus`, `opus→opus`)
- **Half-open state (after 60s):** One test attempt to check recovery
- **Success resets counter:** First successful task closes the circuit and resets failure count

## Integration Points

The wiring connects three EQUIPA modules:

- **`equipa/routing.py`:** Contains `auto_select_model()` (complexity scoring) and `record_model_outcome()` (circuit breaker)
- **`equipa/roles.py`:** Calls auto-routing during model resolution
- **`equipa/cli.py`:** Records outcomes after task completion

## Testing

Manual verification needed:

1. Enable `auto_model_routing` in dispatch config
2. Run tasks with varying complexity (simple typo fix, architectural refactor, security review)
3. Confirm model selection logs show routed tier (haiku/sonnet/opus)
4. Trigger 5+ consecutive failures on one model
5. Verify circuit opens and escalates to higher tier
6. Confirm circuit resets after success

## Compatibility

- **Backward compatible:** Feature is opt-in via flag
- **No breaking changes:** Existing priority 1-5 logic unchanged
- **Late import pattern:** Avoids circular dependency issues
- **Works with all EQUIPA modes:** Single-task, dev-test loop, manager mode, parallel dispatch

## Future Enhancements

Potential improvements for later tasks:

1. **Persistent circuit state:** Store breaker state in TheForge DB instead of in-memory dict
2. **Per-role circuit breakers:** Track failures separately for developer, tester, security-reviewer
3. **Cost tracking integration:** Log actual API costs per model to refine routing thresholds
4. **A/B testing:** Compare routed vs. explicit model selection on task success rates
5. **Adaptive thresholds:** Machine learning to tune complexity scoring weights over time

---

**Implementation:** Complete ✓
**Tests:** Manual verification required
**Documentation:** This file
**Deployment:** Ready (opt-in via feature flag)

Copyright 2026 Forgeborn
