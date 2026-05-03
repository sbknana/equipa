# Config Versioning (Paperclip A1–A3)

The `config_versions` table snapshots tracked configuration files
(`dispatch_config.json`, `forge_config.json`, role prompts under
`prompts/`) so operators can diff and roll back changes.

## Surface

```
equipa --config-cmd snapshot [--config-project N] [-m MSG]
equipa --config-cmd list     [--config-project N]
equipa --config-cmd diff     --config-version-a A --config-version-b B
equipa --config-cmd rollback --config-version ID [--dry-run] [--force]
```

## Auto-snapshot hooks

Two hooks call `config_versions.snapshot()` automatically:

1. **Dispatch entry** — every `equipa` invocation that resolves a
   project (`--task`, `--tasks`, `--goal`, `--auto-run`) snapshots once
   at entry. See `equipa.cli._auto_snapshot_dispatch`.
2. **Heartbeat sweep** — `equipa.heartbeat.run_once` snapshots every
   active project once per sweep, capturing out-of-band edits operators
   make between dispatches.

Both hooks are gated behind `dispatch_config.features.config_versioning`
(default `false`) and dedup on `content_sha` so unchanged configs do
**not** produce new rows. Failures are caught and logged — neither hook
can crash its caller.

## Production / repo asymmetry — IMPORTANT

EQUIPA runs from two trees:

- `Equipa-repo` (this directory) — source of truth, pushes to GitHub.
- `Equipa-prod` — production runtime, pulls from GitHub via
  `scripts/deploy-equipa-prod.sh`.

The auto-snapshot hooks running in production capture the
operator-edited `dispatch_config.json` living in `Equipa-prod`. **A
rollback writes back to the repo path (`REPO_ROOT/dispatch_config.json`),
which does NOT propagate to production by itself.** After running
`equipa --config-cmd rollback ...` in production, the operator must
either:

- copy the rolled-back files into `Equipa-prod` manually, or
- commit them in `Equipa-repo`, push to GitHub, and re-run
  `scripts/deploy-equipa-prod.sh` from `Equipa-prod`.

This asymmetry is intentional: production never flows back into the
repo automatically.

## Feature flag

```json
"features": {
    "config_versioning": false,
    "session_persistence": false,
    "project_templates": false
}
```

Set to `true` in `dispatch_config.json` to enable the auto-snapshot
hooks. The CLI subcommands work regardless of the flag (operators can
always snapshot manually).
