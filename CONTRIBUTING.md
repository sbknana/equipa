# Contributing to EQUIPA

EQUIPA is a multi-agent orchestrator for software-engineering tasks. This guide
gets you from a fresh clone to a green PR. It is meant to be scannable — skim
the headings, run the commands.

## 1. Local setup

Clone the repo, then wire up the repo-tracked git hooks so the pre-commit
checks run on your machine:

```bash
git clone <repo-url> equipa && cd equipa
git config core.hooksPath .githooks
```

That is the whole install. EQUIPA is **pure Python standard library** — there
is nothing to `pip install` and no virtualenv to create (see
[§3, Zero dependencies](#3-zero-dependencies)). You need **Python 3.10+**.

The `core.hooksPath` setting is local to your clone and only needs to be
applied once. It enables the [artifact-hygiene](#4-artifact-hygiene)
pre-commit hook in `.githooks/pre-commit`.

## 2. Running the test suite

The suite is `pytest`-based. It never touches a real TheForge database: point
`THEFORGE_DB` at a throwaway path and every test (and `tests/conftest.py`'s
schema-apply) uses it instead.

```bash
# From the repo root:
THEFORGE_DB="$(mktemp -d)/theforge-test.db" pytest -q
```

For a run that mirrors CI exactly (with the slowest 15 tests reported):

```bash
THEFORGE_DB="$(mktemp -d)/theforge-test.db" pytest -q --durations=15
```

- `pytest` is the **only** test-time dependency. Install it with
  `pip install pytest` (it is not needed to run EQUIPA itself, only to test it).
- Run a single file or test with the usual pytest selectors, e.g.
  `pytest -q tests/test_gen_module_report.py::test_generation_is_deterministic`.

## 3. Zero dependencies

EQUIPA depends only on the Python standard library. This is a hard constraint,
not a preference:

- **No third-party runtime imports.** No `requests`, no `pydantic`, no ORM.
  Use `urllib`, `sqlite3`, `json`, `ast`, `argparse`, `subprocess`, etc.
- **No `pip install` to run.** Copy the `equipa/` folder onto any machine with
  Python 3.10+ and it works. Zero supply-chain surface.
- `pytest` is the single exception, and only for the test suite — never import
  it from `equipa/` or `scripts/` production code.

If a change would introduce a runtime dependency, it will be rejected. Solve
the problem with the stdlib or reconsider the design.

## 4. Artifact hygiene

EQUIPA agents emit transient review/plan files. These must **never** be
committed at the repository root — they make the public repo noisy and can leak
project-internal context. They belong in `.equipa-artifacts/` (gitignored).

The pre-commit hook (`.githooks/pre-commit`, enabled in [§1](#1-local-setup))
rejects a commit that stages any of these at the repo root:

```
SECURITY-REVIEW-*.md   CODE-REVIEW-*.md   PLAN-*.md
RETRY-IMPLEMENTATION-*.md   RETRY-VERIFICATION.md
GEPA-EPISODE-CHECK-*.md   LEAN-CTX-PORT.md
```

The same names inside a subdirectory (e.g. `docs/PLAN-architecture.md`) are
fine — only repo-root droppings are blocked. If the hook fires:

```bash
mkdir -p .equipa-artifacts
mv <file> .equipa-artifacts/
git reset HEAD <file>
```

## 5. Branch & PR conventions

- **Branch** off the default branch (`main`); never commit directly to it.
  Task-driven work uses `forge-task-<id>` branches.
- **Commits** follow Conventional Commits: `feat:`, `fix:`, `refactor:`,
  `test:`, `docs:`, `chore:`. Keep one logical change per commit.
- **Open a PR** against `main`. All CI checks (below) must be green before
  merge. Rebase or update the branch if `main` has moved.

## 6. Continuous integration

Three GitHub Actions workflows gate every push and pull request. Reproduce each
locally before pushing.

| Workflow | File | What it enforces |
|---|---|---|
| Tests | `.github/workflows/tests.yml` | Runs `pytest -q --durations=15` under Python 3.12 with a hermetic `THEFORGE_DB`. The full suite must pass. |
| Docs Drift Check | `.github/workflows/docs-drift-check.yml` | Runs `scripts/check_docs_drift.py`: doc/README path references resolve, the README module & test-badge counts are within tolerance, and the committed module report is not stale (see [§7](#7-generated-docs--drift)). |
| Plugin Boundary Check | `.github/workflows/plugin-boundary-check.yml` | Core `equipa/` must never directly import a plugin package — plugins integrate only through `equipa.plugins` entry points. |

Run the doc-drift check locally with:

```bash
python scripts/check_docs_drift.py --repo-root "$PWD"
```

## 7. Generated docs & drift

Some committed docs are **generated**, not hand-written. Editing them by hand
will fail CI. Regenerate them instead.

### Module dependency report

`equipa/MODULE_DEPENDENCY_REPORT.md` is produced by an AST import-walk of the
`equipa/` package. The generator is **deterministic** (no timestamps, fully
sorted), so re-runs are byte-identical and drift is detectable. After any change
to the package's modules or imports, regenerate it:

```bash
python scripts/gen_module_report.py            # rewrite the report
python scripts/gen_module_report.py --check     # exit 1 if it is stale
```

The Docs Drift Check CI job runs the generator into a buffer and fails if the
committed report differs by a single byte. Do not edit the file by hand.

### Generated files in the gated merge

Every task branch regenerates the report, so in a parallel `--tasks` batch the
second and later branches always conflict in it. Generated files are therefore
declared, with their generator, in one place:
`equipa.generated_files.GENERATED_FILES`. When the orchestrator's gated merge
conflicts **only** in declared files, it regenerates them from the merged tree,
stages them, completes the merge commit and logs
`GATE-AUDIT ... event=generated-files-regenerated files=<paths>`. Any other
conflicting path keeps the ordinary behaviour (abort, `merge_failed`, branch
preserved), and a project without the declared file or generator is unaffected.

The generator is code from the merged tree, which agents can write, so:

- the trust anchor is the default-branch SHA the run's merge guard pinned
  (`DefaultBranchGuard.expected_sha`), passed down to the resolution and never
  re-read from the checkout. The generator runs only if its blob at that
  pinned SHA is identical on the task branch and in the merged tree, and only
  if the merge started from the pinned SHA and HEAD is still there. If the
  default branch moved, nothing runs: the merge is aborted and the guard
  raises the alarm. A branch that changed the generator ends `merge_failed`
  with "the task branch changed the generator"; it is never run. A generator
  updated on the default branch after the task branched is not run either,
  and the reason says "the default branch changed the generator";
- it runs as `python -I` in a private export of its declared `inputs` (and
  itself) from the merged tree, never in the main checkout or the
  orchestrator's process. The export is written straight from git objects
  (`ls-tree` + `cat-file`) and every file is re-hashed against its blob, so
  attributes such as `export-ignore` (in `.gitattributes` or
  `.git/info/attributes`) cannot hide an input, and no archive is unpacked.
  Symlinks or submodules among the inputs, or inputs over the size cap, are
  refused. The generator gets the scrubbed agent environment minus the Claude
  CLI credential, and a timeout that kills its whole process group. It must
  print the file's new content to stdout. Its stdout and stderr are capped
  while they stream: past the cap it is killed at once. A failure, empty
  output, oversized output or timeout ends `merge_failed` with a clean checkout;
- the recorded `merged_sha` is the resolution commit. The resolution is
  refused when the commit is not exactly the tree that was verified or does
  not have the parents (pinned default branch, approved commit). The
  merge-integrity check (task #3116) then accepts it only when its tree equals
  `git merge-tree --write-tree <default> <approved>` everywhere except the
  regenerated paths, which must be exactly that merge's conflicted paths,
  regular files, and the exact blobs of the verified generator output. A
  resolution touching any other path, or carrying any other content, trips
  the guard.

To declare another generated file, add a
`GeneratedFile(path, generator, args, inputs)` entry; the generator must accept
a repository root and print to stdout, and `inputs` lists every path it reads
(empty exports the whole tree).

## 8. Skill manifest integrity

Role prompts and skill files are integrity-protected. `skill_manifest.json`
stores a SHA-256 hash of each prompt/skill file; at runtime
`verify_skill_integrity` compares the files against those hashes and refuses to
run if they were tampered with.

When you **intentionally** change a role prompt or skill file, regenerate the
manifest so its hashes match — otherwise every dispatch will fail integrity
verification:

```bash
python -m equipa --regenerate-manifest
```

Commit the updated `skill_manifest.json` alongside your prompt/skill change. If
you see a runtime warning to *"Run --regenerate-manifest if changes are
intentional,"* this is the command it means.
