# EQUIPA Module Dependency Report

> **Auto-generated — do not edit by hand.**
> Regenerate with `python scripts/gen_module_report.py`. The `docs-drift-check`
> CI job fails if this file is stale (see `scripts/check_docs_drift.py`).

**Modules analyzed:** 57 (equipa/ package, recursive)

## Summary

The `equipa/` package contains **57 Python modules** totaling **42,529 lines** of code across **13 dependency layers** (L0–L12). 33 module(s) use late (deferred) imports to break circular dependencies; there are no top-level circular imports.

## Module Dependency Table

| Module | Lines | Layer | Imports (equipa) | Late Imports | Exports |
|---|---:|:---:|---|---|---:|
| `abort_controller.py` | 218 | L0 | — | — | 3 |
| `agent_launcher.py` | 479 | L0 | — | — | 13 |
| `bash_security.py` | 3954 | L0 | — | — | 5 |
| `classifier.py` | 167 | L0 | — | — | 3 |
| `constants.py` | 337 | L0 | — | — | 67 |
| `env_loader.py` | 315 | L0 | — | `config.py` | 8 |
| `heartbeat.py` | 855 | L0 | — | `config.py`, `config_versions.py`, `db.py`, `sessions.py` | 18 |
| `hooks/classifier_retry.py` | 105 | L0 | — | — | 3 |
| `hooks/dispatcher.py` | 151 | L0 | — | — | 5 |
| `hooks/security_review_gate.py` | 94 | L0 | — | — | 4 |
| `hooks/vacuous_pass.py` | 308 | L0 | — | `monitoring.py` | 3 |
| `initiative.py` | 763 | L0 | — | `security.py` | 19 |
| `integration_test.py` | 194 | L0 | — | `git_ops.py` | 5 |
| `mcp_health.py` | 112 | L0 | — | — | 5 |
| `plugins.py` | 69 | L0 | — | — | 3 |
| `reactive_check.py` | 322 | L0 | — | `bash_security.py` | 6 |
| `redact.py` | 260 | L0 | — | — | 5 |
| `scaffold.py` | 633 | L0 | — | `db.py` | 8 |
| `tool_result_storage.py` | 247 | L0 | — | — | 15 |
| `checkpoints.py` | 308 | L1 | `constants.py` | — | 8 |
| `config.py` | 515 | L1 | `constants.py` | — | 23 |
| `db.py` | 798 | L1 | `constants.py` | `config.py`, `output.py`, `prompts.py`, `tasks.py` | 13 |
| `hooks/__init__.py` | 441 | L1 | `hooks/dispatcher.py` | `config.py`, `env_loader.py` | 17 |
| `output.py` | 292 | L1 | `constants.py` | `monitoring.py` | 7 |
| `role_resolver.py` | 643 | L1 | `constants.py` | `config.py`, `git_ops.py` | 22 |
| `routing.py` | 553 | L1 | `constants.py` | — | 29 |
| `security.py` | 137 | L1 | `constants.py` | — | 4 |
| `config_versions.py` | 480 | L2 | `constants.py`, `db.py` | — | 8 |
| `embeddings.py` | 233 | L2 | `config.py`, `constants.py` | `db.py`, `graph.py` | 6 |
| `flows.py` | 762 | L2 | `config.py`, `db.py` | `sessions.py` | 25 |
| `git_ops.py` | 1568 | L2 | `constants.py`, `role_resolver.py` | `config.py`, `tasks.py` | 38 |
| `graph.py` | 321 | L2 | `db.py` | — | 7 |
| `initiative_runner.py` | 1380 | L2 | `initiative.py`, `security.py` | `db.py`, `dispatch.py`, `git_ops.py` | 29 |
| `messages.py` | 124 | L2 | `db.py` | `security.py` | 4 |
| `roles.py` | 264 | L2 | `config.py`, `constants.py` | `output.py`, `role_resolver.py`, `routing.py`, `tasks.py` | 3 |
| `sessions.py` | 335 | L2 | `checkpoints.py`, `db.py` | `agent_runner.py` | 7 |
| `tasks.py` | 335 | L2 | `constants.py`, `db.py` | — | 8 |
| `templates.py` | 943 | L2 | `db.py` | `constants.py`, `embeddings.py` | 8 |
| `mcp_server.py` | 960 | L3 | `config.py`, `constants.py`, `tasks.py` | — | 13 |
| `merge_safety.py` | 293 | L3 | `db.py`, `git_ops.py` | — | 13 |
| `monitoring.py` | 894 | L3 | `constants.py`, `git_ops.py`, `hooks/__init__.py` | `parsing.py` | 12 |
| `parsing.py` | 903 | L3 | `constants.py`, `git_ops.py` | `tool_result_storage.py` | 20 |
| `security_gate.py` | 1089 | L3 | `git_ops.py` | `db.py` | 42 |
| `single_agent_guard.py` | 632 | L3 | `git_ops.py` | `role_resolver.py` | 6 |
| `lessons.py` | 723 | L4 | `config.py`, `constants.py`, `db.py`, `parsing.py` | `embeddings.py`, `graph.py`, `output.py`, `security.py` | 13 |
| `merge_integrity.py` | 962 | L4 | `git_ops.py`, `security_gate.py` | — | 22 |
| `rlm_decompose.py` | 744 | L4 | `config.py`, `env_loader.py`, `output.py`, `parsing.py` | `agent_runner.py` | 21 |
| `prompts.py` | 871 | L5 | `config.py`, `constants.py`, `lessons.py`, `parsing.py`, `security.py` | `db.py`, `git_ops.py`, `graph.py`, `initiative.py`, `role_resolver.py` | 8 |
| `reflexion.py` | 158 | L5 | `config.py`, `db.py`, `lessons.py`, `output.py`, `parsing.py` | `agent_runner.py` | 4 |
| `agent_runner.py` | 3047 | L6 | `abort_controller.py`, `agent_launcher.py`, `checkpoints.py`, `config.py`, `constants.py`, `db.py`, `env_loader.py`, `monitoring.py`, `output.py`, `parsing.py`, `prompts.py`, `reactive_check.py`, `redact.py`, `security.py`, `tasks.py` | `cli.py`, `git_ops.py`, `rlm_decompose.py`, `role_resolver.py` | 26 |
| `preflight.py` | 347 | L7 | `agent_runner.py`, `constants.py`, `env_loader.py`, `output.py` | `prompts.py`, `roles.py` | 3 |
| `loops.py` | 3749 | L8 | `agent_runner.py`, `checkpoints.py`, `classifier.py`, `config.py`, `constants.py`, `db.py`, `git_ops.py`, `hooks/__init__.py`, `merge_integrity.py`, `messages.py`, `monitoring.py`, `output.py`, `parsing.py`, `preflight.py`, `prompts.py`, `roles.py`, `security_gate.py`, `sessions.py`, `tasks.py` | `role_resolver.py` | 23 |
| `manager.py` | 513 | L9 | `agent_runner.py`, `constants.py`, `db.py`, `git_ops.py`, `loops.py`, `merge_integrity.py`, `merge_safety.py`, `output.py`, `prompts.py`, `roles.py`, `single_agent_guard.py`, `tasks.py` | `dispatch.py` | 9 |
| `dispatch.py` | 4114 | L10 | `agent_runner.py`, `config.py`, `constants.py`, `db.py`, `git_ops.py`, `hooks/__init__.py`, `lessons.py`, `loops.py`, `manager.py`, `merge_integrity.py`, `merge_safety.py`, `output.py`, `parsing.py`, `prompts.py`, `reflexion.py`, `roles.py`, `routing.py`, `security_gate.py`, `single_agent_guard.py`, `tasks.py` | `flows.py`, `initiative.py`, `role_resolver.py`, `scaffold.py` | 30 |
| `cli.py` | 2441 | L11 | `agent_runner.py`, `checkpoints.py`, `config.py`, `constants.py`, `db.py`, `dispatch.py`, `git_ops.py`, `hooks/__init__.py`, `lessons.py`, `loops.py`, `manager.py`, `mcp_server.py`, `merge_integrity.py`, `merge_safety.py`, `monitoring.py`, `output.py`, `parsing.py`, `plugins.py`, `prompts.py`, `reflexion.py`, `roles.py`, `routing.py`, `security.py`, `security_gate.py`, `single_agent_guard.py`, `tasks.py`, `templates.py` | `config_versions.py`, `initiative_runner.py`, `role_resolver.py`, `scaffold.py` | 21 |
| `__init__.py` | 58 | L12 | `cli.py`, `dispatch.py`, `loops.py`, `manager.py`, `mcp_server.py`, `monitoring.py`, `prompts.py` | — | 14 |
| `__main__.py` | 16 | L12 | `cli.py` | — | 0 |

**Total:** 42,529 lines | 764 public exports

## Modules by Layer

Layer *N* is the longest chain of top-level (non-deferred) imports from a leaf module. Late imports are excluded so the graph stays acyclic.

- **L0**: `abort_controller.py`, `agent_launcher.py`, `bash_security.py`, `classifier.py`, `constants.py`, `env_loader.py`, `heartbeat.py`, `hooks/classifier_retry.py`, `hooks/dispatcher.py`, `hooks/security_review_gate.py`, `hooks/vacuous_pass.py`, `initiative.py`, `integration_test.py`, `mcp_health.py`, `plugins.py`, `reactive_check.py`, `redact.py`, `scaffold.py`, `tool_result_storage.py`
- **L1**: `checkpoints.py`, `config.py`, `db.py`, `hooks/__init__.py`, `output.py`, `role_resolver.py`, `routing.py`, `security.py`
- **L2**: `config_versions.py`, `embeddings.py`, `flows.py`, `git_ops.py`, `graph.py`, `initiative_runner.py`, `messages.py`, `roles.py`, `sessions.py`, `tasks.py`, `templates.py`
- **L3**: `mcp_server.py`, `merge_safety.py`, `monitoring.py`, `parsing.py`, `security_gate.py`, `single_agent_guard.py`
- **L4**: `lessons.py`, `merge_integrity.py`, `rlm_decompose.py`
- **L5**: `prompts.py`, `reflexion.py`
- **L6**: `agent_runner.py`
- **L7**: `preflight.py`
- **L8**: `loops.py`
- **L9**: `manager.py`
- **L10**: `dispatch.py`
- **L11**: `cli.py`
- **L12**: `__init__.py`, `__main__.py`

## Late Import Inventory

33 module(s) defer intra-package imports inside function bodies to avoid import cycles:

| Module | Late Imports |
|---|---|
| `agent_runner.py` | `cli.py`, `git_ops.py`, `rlm_decompose.py`, `role_resolver.py` |
| `cli.py` | `config_versions.py`, `initiative_runner.py`, `role_resolver.py`, `scaffold.py` |
| `db.py` | `config.py`, `output.py`, `prompts.py`, `tasks.py` |
| `dispatch.py` | `flows.py`, `initiative.py`, `role_resolver.py`, `scaffold.py` |
| `embeddings.py` | `db.py`, `graph.py` |
| `env_loader.py` | `config.py` |
| `flows.py` | `sessions.py` |
| `git_ops.py` | `config.py`, `tasks.py` |
| `heartbeat.py` | `config.py`, `config_versions.py`, `db.py`, `sessions.py` |
| `hooks/__init__.py` | `config.py`, `env_loader.py` |
| `hooks/vacuous_pass.py` | `monitoring.py` |
| `initiative.py` | `security.py` |
| `initiative_runner.py` | `db.py`, `dispatch.py`, `git_ops.py` |
| `integration_test.py` | `git_ops.py` |
| `lessons.py` | `embeddings.py`, `graph.py`, `output.py`, `security.py` |
| `loops.py` | `role_resolver.py` |
| `manager.py` | `dispatch.py` |
| `messages.py` | `security.py` |
| `monitoring.py` | `parsing.py` |
| `output.py` | `monitoring.py` |
| `parsing.py` | `tool_result_storage.py` |
| `preflight.py` | `prompts.py`, `roles.py` |
| `prompts.py` | `db.py`, `git_ops.py`, `graph.py`, `initiative.py`, `role_resolver.py` |
| `reactive_check.py` | `bash_security.py` |
| `reflexion.py` | `agent_runner.py` |
| `rlm_decompose.py` | `agent_runner.py` |
| `role_resolver.py` | `config.py`, `git_ops.py` |
| `roles.py` | `output.py`, `role_resolver.py`, `routing.py`, `tasks.py` |
| `scaffold.py` | `db.py` |
| `security_gate.py` | `db.py` |
| `sessions.py` | `agent_runner.py` |
| `single_agent_guard.py` | `role_resolver.py` |
| `templates.py` | `constants.py`, `embeddings.py` |

## Import Count per Module

How many other `equipa` modules each module imports.

| Module | Top-level | Late | Total |
|---|---:|---:|---:|
| `__init__.py` | 7 | 0 | **7** |
| `__main__.py` | 1 | 0 | **1** |
| `abort_controller.py` | 0 | 0 | **0** |
| `agent_launcher.py` | 0 | 0 | **0** |
| `agent_runner.py` | 15 | 4 | **19** |
| `bash_security.py` | 0 | 0 | **0** |
| `checkpoints.py` | 1 | 0 | **1** |
| `classifier.py` | 0 | 0 | **0** |
| `cli.py` | 27 | 4 | **31** |
| `config.py` | 1 | 0 | **1** |
| `config_versions.py` | 2 | 0 | **2** |
| `constants.py` | 0 | 0 | **0** |
| `db.py` | 1 | 4 | **5** |
| `dispatch.py` | 20 | 4 | **24** |
| `embeddings.py` | 2 | 2 | **4** |
| `env_loader.py` | 0 | 1 | **1** |
| `flows.py` | 2 | 1 | **3** |
| `git_ops.py` | 2 | 2 | **4** |
| `graph.py` | 1 | 0 | **1** |
| `heartbeat.py` | 0 | 4 | **4** |
| `hooks/__init__.py` | 1 | 2 | **3** |
| `hooks/classifier_retry.py` | 0 | 0 | **0** |
| `hooks/dispatcher.py` | 0 | 0 | **0** |
| `hooks/security_review_gate.py` | 0 | 0 | **0** |
| `hooks/vacuous_pass.py` | 0 | 1 | **1** |
| `initiative.py` | 0 | 1 | **1** |
| `initiative_runner.py` | 2 | 3 | **5** |
| `integration_test.py` | 0 | 1 | **1** |
| `lessons.py` | 4 | 4 | **8** |
| `loops.py` | 19 | 1 | **20** |
| `manager.py` | 12 | 1 | **13** |
| `mcp_health.py` | 0 | 0 | **0** |
| `mcp_server.py` | 3 | 0 | **3** |
| `merge_integrity.py` | 2 | 0 | **2** |
| `merge_safety.py` | 2 | 0 | **2** |
| `messages.py` | 1 | 1 | **2** |
| `monitoring.py` | 3 | 1 | **4** |
| `output.py` | 1 | 1 | **2** |
| `parsing.py` | 2 | 1 | **3** |
| `plugins.py` | 0 | 0 | **0** |
| `preflight.py` | 4 | 2 | **6** |
| `prompts.py` | 5 | 5 | **10** |
| `reactive_check.py` | 0 | 1 | **1** |
| `redact.py` | 0 | 0 | **0** |
| `reflexion.py` | 5 | 1 | **6** |
| `rlm_decompose.py` | 4 | 1 | **5** |
| `role_resolver.py` | 1 | 2 | **3** |
| `roles.py` | 2 | 4 | **6** |
| `routing.py` | 1 | 0 | **1** |
| `scaffold.py` | 0 | 1 | **1** |
| `security.py` | 1 | 0 | **1** |
| `security_gate.py` | 1 | 1 | **2** |
| `sessions.py` | 2 | 1 | **3** |
| `single_agent_guard.py` | 1 | 1 | **2** |
| `tasks.py` | 2 | 0 | **2** |
| `templates.py` | 1 | 2 | **3** |
| `tool_result_storage.py` | 0 | 0 | **0** |
