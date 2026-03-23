# Feature Flags -- Task 1587

## Summary

Added feature flags to `dispatch_config.json` and wired them into all injection points in `build_system_prompt()` and other callsites.

## What Was Already Done (prior work)

The bulk of the feature flags system was already implemented:

1. **`DEFAULT_FEATURE_FLAGS`** dict (9 flags) in `forge_orchestrator.py:6110-6120`
2. **`is_feature_enabled()`** function with fallback chain: config -> defaults -> False
3. **`load_dispatch_config()`** with deep-merge so partial feature overrides don't wipe other flags
4. **`dispatch_config.example.json`** already had the `features` section with all 9 flags
5. **Test suite** at `tests/test_feature_flags.py` (14 tests covering all scenarios)
6. **Gating in place** for: `forgesmith_lessons` (line 2235), `forgesmith_episodes` (line 2253), `quality_scoring` (line 735), `security_review` (line 7158), `anti_compaction_state` (line 4353), `gepa_ab_testing` (line 2207)

## What This Task Fixed

**Missing gate on `language_prompts`**: The language-specific prompt injection block in `build_system_prompt()` (lines 2293-2316) was NOT gated by the `language_prompts` feature flag. It always ran regardless of the flag value.

**Fix**: Added `is_feature_enabled(dispatch_config, "language_prompts")` check to the condition on line 2295, so language detection and prompt injection only runs when the flag is enabled.

## Feature Flags Reference

| Flag | Default | Gated At |
|------|---------|----------|
| `language_prompts` | `true` | `build_system_prompt()` -- language detection + injection |
| `hooks` | `false` | Future -- not yet implemented |
| `mcp_health` | `false` | Future -- not yet implemented |
| `forgesmith_lessons` | `true` | `build_system_prompt()` -- lesson injection |
| `forgesmith_episodes` | `true` | `build_system_prompt()` -- episode injection |
| `gepa_ab_testing` | `false` | `build_system_prompt()` -- GEPA A/B prompt variant |
| `security_review` | `true` | `run_single_task()` + `run_security_review()` |
| `quality_scoring` | `true` | `run_quality_scoring()` |
| `anti_compaction_state` | `true` | Agent loop -- compaction history injection |

## How to Use

Toggle any feature in `dispatch_config.json`:

```json
{
  "features": {
    "language_prompts": false
  }
}
```

Deep-merge means you only need to specify flags you want to override -- all others keep their defaults.

## Test Results

302 tests passed (including 14 feature flag tests), 0 failures.
