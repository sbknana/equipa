# EQUIPA Project Template Spec (v1.0)

This document defines the on-disk contract that any orchestrator runtime —
including non-Claude adapters (Codex, Cursor, OpenCode, custom Ollama
agents) — must honor when producing or consuming an EQUIPA project
template archive. The reference implementation lives in
`equipa/templates.py` (exporter half, see PLAN-1067 §3.C1).

The goals of the spec are:

1. Move project state (tasks, decisions, lessons) and on-disk assets
   between hosts without coupling either side to a specific LLM provider.
2. Stay reproducible: every byte under `tables/` and `assets/` is
   covered by a SHA-256 in the manifest.
3. Stay safe: secrets and host-local state never travel inside an
   archive.

---

## 1. Layout

```
<archive_root>/
  manifest.json
  tables/
    projects.jsonl
    tasks.jsonl
    decisions.jsonl
    session_notes.jsonl
    open_questions.jsonl
    lessons_learned.jsonl
    agent_episodes.jsonl    # optional in spec, written by reference exporter
    agent_runs.jsonl        # optional in spec, written by reference exporter
  assets/
    CLAUDE.md               # if present in the source project working dir
    prompts/                # per-project prompt overrides if any
    ...
```

The archive root may be either a directory or a `.tar.gz` whose top-level
member is the same directory. The `archive=True` mode of the reference
exporter produces the latter.

## 2. Required tables

Every conformant template MUST contain these table files under
`tables/` (each is a JSON-Lines file, one row per line):

| Table             | Purpose                                       |
|-------------------|-----------------------------------------------|
| `projects`        | The single source project row (1 row).        |
| `tasks`           | All tasks scoped to the project.              |
| `decisions`       | All decisions scoped to the project.          |
| `session_notes`   | All session notes scoped to the project.     |
| `open_questions`  | All open questions scoped to the project.    |
| `lessons_learned` | All lessons scoped to the project.            |

A table file MAY contain zero rows but the file itself MUST exist (so
importers can compute SHA-256 against the manifest unconditionally).

## 3. Optional tables

For v1.0 there are no spec-required optional tables. The reference
exporter additionally writes `agent_episodes.jsonl` and
`agent_runs.jsonl` because they carry useful learning-loop signal;
importers MAY ignore them.

Future spec versions may promote tables from optional to required —
manifest `version` is bumped on any such change.

## 4. Forbidden tables (NEVER export)

The following tables MUST NEVER appear inside a template archive,
regardless of operator flags:

| Table            | Reason                                       |
|------------------|----------------------------------------------|
| `api_keys`       | Host-local secrets; sharing is a vuln.       |
| `model_registry` | Host-local model metadata, not portable.     |

Conformant exporters MUST refuse to write these tables. Conformant
importers MUST reject any archive whose `manifest.table_list` contains
either name.

## 5. Manifest

`manifest.json` is a JSON object with these required fields:

| Field            | Type    | Description                                                         |
|------------------|---------|---------------------------------------------------------------------|
| `version`        | string  | Spec version, e.g. `"1.0"`.                                         |
| `exported_at`    | string  | ISO-8601 UTC timestamp of export.                                   |
| `source_runtime` | string  | Identifier of the producing runtime, e.g. `"equipa-py"`.            |
| `id_namespace`   | string  | Always `"source"` in v1.0 — IDs are source-DB integers, importer remaps. |
| `table_list`     | array   | Ordered list of table names corresponding to `tables/<name>.jsonl`. |
| `row_counts`     | object  | Map of table name → integer row count.                              |
| `file_sha`       | object  | Map of relative file path → hex SHA-256 of file content.            |

Optional but commonly written fields:

| Field               | Type    | Description                                                      |
|---------------------|---------|------------------------------------------------------------------|
| `project_id_source` | integer | The original `projects.id` in the source DB (informational).     |
| `scrub_costs`       | bool    | Whether `agent_runs.cost_usd` was nulled during export.          |

### 5.1 Auth-agnostic constraint (HARD)

The manifest MUST NOT contain ANY of the following keys (or
case-insensitive variants):

- `auth_mode`
- `auth`
- `api_key_provider`

Auth mode (Max-subscription vs API key vs other) is a property of the
**orchestrator host** (read from `dispatch_config.json` at runtime),
not of the template. EQUIPA software supports both Max-subscription
and API-key auth modes, and a template exported from a Max-subscription
host MUST be importable into an API-key host without modification, and
vice versa.

Conformant validators MUST reject any manifest that violates this
constraint.

### 5.2 No Claude-specific leakage

The manifest MUST NOT contain any string (key or value) whose
case-insensitive form contains the substrings `claude`, `opus`,
`sonnet`, `haiku`, or `claude_session_id`. The reference validator
(`templates.validate_manifest`) enforces this.

Note: the literal value `"equipa-py"` is the canonical
`source_runtime` from the reference exporter and obviously contains
none of those substrings. Other runtimes are free to use their own
names provided they do not leak Claude tokens.

## 6. Hashing

- Algorithm: **SHA-256** (hex digest, lowercase).
- Scope: the raw byte content of each file under `tables/` and
  `assets/`. The manifest itself is NOT hashed inside `file_sha`
  (that would be self-referential).
- Manifest entries: keys are POSIX-style relative paths from the
  archive root (e.g. `tables/tasks.jsonl`, `assets/CLAUDE.md`).
- Importers SHOULD verify every hash before applying any DB writes.

## 7. Foreign-key remapping

All integer IDs in `tables/*.jsonl` are written in the **source
namespace** (the source DB's `id` column values). Importers MUST:

1. Walk tables in dependency order. The recommended order is the
   `EXPORTED_TABLES` constant in `equipa/templates.py`:
   `projects → tasks → decisions → session_notes → open_questions →
   lessons_learned → agent_episodes → agent_runs`.
2. Maintain a remap dictionary keyed by `(table_name, source_id)`
   that yields the new local ID assigned at insert time.
3. Rewrite every FK column (e.g. `tasks.project_id`,
   `decisions.resolved_by_task_id`, `agent_runs.task_id`) from the
   remap before insert.
4. NEVER reuse a source ID directly — local DBs may already have a
   row at that ID for a different project.

## 8. Embeddings

`lessons_learned` rows carry an `embedding` column when populated by
the source vector-memory subsystem. Per operator decision, exporters
write the embedding **as-is**; the importer (C2) decides whether to
re-embed using the target host's local model.

## 9. Cost scrubbing

When the operator passes `--scrub-costs` (exporter `scrub_costs=True`),
the `cost_usd` column of every exported `agent_runs` row MUST be set
to `null`. All other columns are preserved. The manifest records
`scrub_costs: true` so downstream auditors can see that costs were
intentionally removed (rather than zero).

## 10. Versioning

The manifest `version` field is the spec version, not the producing
runtime version. Breaking changes (rename or remove a required field,
add a required field, change `id_namespace` semantics, change
hashing algorithm) bump the major version. Additive changes (new
optional fields, new optional tables) bump the minor version.
