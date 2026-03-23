# Feature Flags — Task 1587

## Summary

Added the `features` section to `dispatch_config.example.json` and fixed a shallow-merge bug in `load_dispatch_config()` that would wipe unspecified feature flags when a user provided a partial override.

## Changes

### 1. `dispatch_config.example.json` — Added features section

```json
"features": {
    "language_prompts": true,
    "hooks": false,
    "mcp_health": false,
    "forgesmith_lessons": true,
    "forgesmith_episodes": true,
    "gepa_ab_testing": false,
    "security_review": true,
    "quality_scoring": true,
    "anti_compaction_state": true
}
```

All 9 flags are documented in the example config so users can toggle features without reading source code.

### 2. `forge_orchestrator.py` — Fixed deep merge of features dict

**Bug found:** `load_dispatch_config()` used shallow key assignment (`config[key] = data[key]`) for the `features` dict. If a user's config contained `{"features": {"hooks": true}}`, the merge would **replace** the entire features dict, losing all 8 other default flags.

**Fix:** After the initial merge loop, the features dict is explicitly deep-merged:
```python
if "features" in data and isinstance(data["features"], dict):
    merged_features = dict(DEFAULT_FEATURE_FLAGS)
    merged_features.update(data["features"])
    config["features"] = merged_features
```

This ensures partial overrides work correctly — specifying one flag preserves all others.

### 3. `tests/test_feature_flags.py` — 14 new tests

| Test Class | Count | Coverage |
|---|---|---|
| `TestIsFeatureEnabled` | 7 | None config, config override, fallback, unknown keys, empty dict |
| `TestDefaultFeatureFlags` | 2 | All 9 flags present with expected values |
| `TestLoadDispatchConfigDeepMerge` | 4 | Partial override, no features key, full override, missing file |
| `TestExampleConfigMatchesDefaults` | 1 | Example JSON matches code defaults |

All 14 tests pass.

## Feature Flag Injection Points (already gated)

| Flag | Location | Line |
|---|---|---|
| `gepa_ab_testing` | `build_system_prompt()` | 2207 |
| `forgesmith_lessons` | `build_system_prompt()` | 2235 |
| `forgesmith_episodes` | `build_system_prompt()` | 2253 |
| `quality_scoring` | `run_quality_scoring()` | 735 |
| `anti_compaction_state` | agent run loop | 4328 |
| `security_review` | post-task review | 7049 |
| `language_prompts` | *(future — no injection point yet)* | — |
| `hooks` | *(future — no injection point yet)* | — |
| `mcp_health` | *(future — no injection point yet)* | — |

All active features were already properly gated by `is_feature_enabled()` before this task. The three future flags (`language_prompts`, `hooks`, `mcp_health`) are defined in `DEFAULT_FEATURE_FLAGS` and ready for implementation.
