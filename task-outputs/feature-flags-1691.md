# Feature Flag Registration - Task 1691

**Date:** 2026-03-27
**Project:** EQUIPA (Project ID: 23)
**Task:** Add 3 new feature flags and ollama_embedding_model configuration

## Summary

Successfully added 3 new feature flags to EQUIPA's dispatch configuration system:
- `vector_memory`: False (disabled by default)
- `auto_model_routing`: False (disabled by default)
- `knowledge_graph`: False (disabled by default)

Also added `ollama_embedding_model` configuration parameter with default value `all-MiniLM-L6-v2`.

## Changes Made

### 1. equipa/dispatch.py
Updated `DEFAULT_FEATURE_FLAGS` dictionary to include 3 new flags (lines 64-75):
```python
DEFAULT_FEATURE_FLAGS: dict[str, bool] = {
    "language_prompts": True,
    "hooks": False,
    "mcp_health": False,
    "forgesmith_lessons": True,
    "forgesmith_episodes": True,
    "gepa_ab_testing": False,
    "security_review": True,
    "quality_scoring": True,
    "anti_compaction_state": True,
    "vector_memory": False,           # NEW
    "auto_model_routing": False,      # NEW
    "knowledge_graph": False,         # NEW
}
```

### 2. dispatch_config.example.json
Added flags to `features` object and new `ollama_embedding_model` parameter:
```json
{
  "features": {
    ...existing flags...,
    "vector_memory": false,
    "auto_model_routing": false,
    "knowledge_graph": false
  },
  ...
  "ollama_embedding_model": "all-MiniLM-L6-v2",
  ...
}
```

### 3. tests/test_feature_flags.py
Updated test expectations:
- Changed docstring from "9 expected flags" to "12 expected flags"
- Added 3 new flags to `EXPECTED_FLAGS` dictionary in `TestDefaultFeatureFlags` class
- All 14 tests pass successfully

## Test Results

```
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.0.2, pluggy-1.6.0
collected 14 items

tests/test_feature_flags.py::TestIsFeatureEnabled::test_config_without_features_key_uses_defaults PASSED
tests/test_feature_flags.py::TestIsFeatureEnabled::test_empty_features_dict_uses_defaults PASSED
tests/test_feature_flags.py::TestIsFeatureEnabled::test_falls_back_to_default_for_missing_feature PASSED
tests/test_feature_flags.py::TestIsFeatureEnabled::test_none_config_returns_default_false PASSED
tests/test_feature_flags.py::TestIsFeatureEnabled::test_none_config_returns_default_true PASSED
tests/test_feature_flags.py::TestIsFeatureEnabled::test_reads_from_config_features PASSED
tests/test_feature_flags.py::TestIsFeatureEnabled::test_unknown_feature_defaults_false PASSED
tests/test_feature_flags.py::TestDefaultFeatureFlags::test_contains_all_expected_flags PASSED
tests/test_feature_flags.py::TestDefaultFeatureFlags::test_flag_values_match PASSED
tests/test_feature_flags.py::TestLoadDispatchConfigDeepMerge::test_full_features_override PASSED
tests/test_feature_flags.py::TestLoadDispatchConfigDeepMerge::test_missing_file_returns_defaults PASSED
tests/test_feature_flags.py::TestLoadDispatchConfigDeepMerge::test_no_features_key_uses_all_defaults PASSED
tests/test_feature_flags.py::TestLoadDispatchConfigDeepMerge::test_partial_features_preserves_other_defaults PASSED
tests/test_feature_flags.py::TestExampleConfigMatchesDefaults::test_example_features_match_code_defaults PASSED

============================== 14 passed in 0.05s ==============================
```

## Git Commits

1. `aec78e2` - feat: add 3 new feature flags to dispatch.py
2. `7b92ec1` - feat: add 3 feature flags and ollama_embedding_model to example config
3. `a4a70d3` - test: update test_feature_flags to expect 12 flags

## Feature Flag Descriptions

### vector_memory (disabled)
Placeholder for future vector-based memory system integration. When enabled, will allow agents to store and retrieve context using semantic embeddings.

### auto_model_routing (disabled)
Placeholder for automatic model selection based on task complexity. When enabled, EQUIPA can dynamically route tasks to optimal models (Opus/Sonnet/Haiku) based on requirements.

### knowledge_graph (disabled)
Placeholder for knowledge graph integration. When enabled, will build and query relationship graphs between code entities, tasks, and decisions.

## Configuration Notes

The new flags follow EQUIPA's existing pattern:
- All 3 flags default to `False` (disabled)
- Can be overridden in user's `dispatch_config.json`
- Accessible via `is_feature_enabled(dispatch_config, "flag_name")`
- Deep-merged with defaults during config load

The `ollama_embedding_model` parameter allows users to specify which Ollama model to use for embeddings when `vector_memory` is eventually implemented. The default `all-MiniLM-L6-v2` is a popular open-source embedding model optimized for semantic similarity tasks.

## Status

✅ All changes implemented
✅ All tests passing
✅ All commits made
✅ Documentation complete
