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

The suite is `pytest`-based. It never touches a real TheForge database:
`tests/conftest.py` points `THEFORGE_DB` at a throwaway file in a private temp
directory before any `equipa` module is imported, whatever the environment
says, and refuses to run if a loaded module still points elsewhere.

The full suite runs in parallel with pytest-xdist. Serially it takes over ten
minutes; in parallel it takes under four on a 16-core host. This is the
default invocation, used by CI and by EQUIPA's tester and developer agents:

```bash
# From the repo root:
timeout 540 python3 -m pytest -q -p no:cacheprovider -n auto --dist loadfile
```

- `--dist loadfile` keeps every test file on one worker, in file order.
- Every worker is a separate process with its own test DB and temp directory
  (`tests/conftest.py`), so workers never share database state. Under xdist,
  test-DB paths are checked against the controller's session directory, which
  holds every worker's DB and `tmp_path`.
- `timeout 540` keeps the run inside the 10-minute limit of an agent's Bash
  call. Run it in the foreground.
- Report the counts from pytest's final summary line (passed / skipped / total).
  The suite must have 0 skipped tests.
- Add `--durations=25` to list the slowest tests. No single test should take
  more than 20 seconds.
- Time a budget on in-process work (a parser or regex "runs in half a
  second") with `time.process_time()`, not `time.perf_counter()`. Every core
  runs a worker, so a wall clock also counts the time a worker waits for a
  core, and such budgets fail whenever the host is busy. Keep the wall clock
  only for what really waits: subprocesses, threads, sockets, timeouts.
- A timing test proves that work grows linearly, not that the machine is
  fast. Check it through `tests/host_timing.py`: `assert_linear_time` holds
  the work to its budget times a host factor, and times the same shape at
  the test's size and a quarter of it, failing at a growth ratio of 8 or
  more (linear is about 4, quadratic 16). The factor is measured around
  each measurement over its base budget: that measurement is taken again
  with a fixed reference workload run in the same process right before and
  right after it, so contention between xdist workers loosens the budget
  of exactly the work it slowed. A measurement within its base budget
  passes at any factor and runs no reference. A faster host never tightens
  a budget. A ratio of sub-millisecond times is noise: the smaller time is
  raised to 20 ms, and a reading that could still reach the limit is
  measured repeatedly (or, for a regex scan, made larger) until the
  smaller size takes 20 ms. Never raise a base budget. Build the input
  before the clock starts: allocating a multi-megabyte string is page
  faults, which grew 32x from 1 MB to 4 MB under load. `assert_linear_time`
  pauses the garbage collector inside each timed call: a full collection
  walks the heap a long xdist worker has built up, and it paused a linear
  scan's larger size 0.1 s.
- The factor is capped at 4.0. A budget loosened further would hide a
  linear slowdown as large (growth only sees superlinear work), so a
  measurement on a host or load more than 4x slower than the development
  host fails with `HostTooSlowError` instead of passing.
- A bound that tells "returned at once" from "waited out a timeout" is not
  scaled; list it in `DEADLINE_TESTS` in `tests/test_host_timing_3171.py`.
  A whole-corpus cap held to `budget()` without a growth check goes in
  `CORPUS_CAPS`. That fence fails on any other timing test (clock
  differences, `*_ns` clocks, `timeit`, timer objects, event-loop
  heartbeats) that reaches no growth check.
- The helper raises its failures explicitly, so `python -O` cannot strip
  them.
- Every test phase has a deadline (`tests/deadline_watchdog.py`): 600 s of
  CPU time, 300 s for a module that imports `tests/host_timing.py`, or
  `@pytest.mark.deadline(seconds)`. Past it the test fails with
  `DeadlineExceeded`; a test that swallows that, or waits without using
  CPU, is stopped once the wall clock passes the deadline plus a grace, and
  its traceback is dumped. The grace is four times the deadline, at least
  60 s and at most 300 s, so a worker that a busy host gives a slice of a
  core still fails by name instead of crashing. That stop is a POSIX timer
  signal, not a thread, so it costs a worker no task under a task cap
  (systemd `TasksMax`); it falls back to faulthandler's watchdog thread off
  64-bit Linux. After one
  test of a worker has run past its deadline, every later test of that
  worker gets 30 s, so a regression that hangs a whole module costs one
  deadline and then 30 s per test. A hung regression fails by name instead
  of timing out the CI job.
- To check the timing tests under contention, run them at `-n 16` while a
  CPU-burning process runs. Under a task cap, leave git room: at `-n 16`
  the xdist workers alone hold about 50 tasks, and the files holding the
  timing tests peak near 70 because `git fetch` starts index-pack threads,
  so a `TasksMax=64` scope fails those git tests with "unable to create
  thread" (main does too). The timing tests on their own fit in 64.
- Set `EQUIPA_TIMING_HOST_FACTOR` (a number > 0, at most 4.0) to force the
  host factor, e.g. `EQUIPA_TIMING_HOST_FACTOR=2.0` to run the timing tests
  as on a runner twice as slow. The session-start factor is printed in the
  pytest header.
- Run a single file or test without `-n`, e.g.
  `python3 -m pytest -q -p no:cacheprovider tests/test_gen_module_report.py::test_generation_is_deterministic`.

To prove a parallel run matches a serial one (same test ids, same outcomes,
nothing skipped), save both with `--junitxml` and compare them. The serial
run is too long for one agent Bash call, so it runs in two parts of about six
to seven minutes each, split by test-file name; pass every part:

```bash
timeout 540 python3 -m pytest -q -p no:cacheprovider \
    --junitxml=serial-1.xml tests/test_[a-l]*.py
timeout 540 python3 -m pytest -q -p no:cacheprovider \
    --junitxml=serial-2.xml tests/orchestrator tests/test_[m-z]*.py
timeout 540 python3 -m pytest -q -p no:cacheprovider -n auto --dist loadfile \
    --junitxml=parallel.xml
python3 scripts/compare_junit_runs.py --serial serial-1.xml serial-2.xml \
    --parallel parallel.xml
```

A test file the two parts miss shows up as a difference: the parallel run
reports test ids the serial parts do not.

- The test dependencies are `pytest`, `pytest-asyncio` and `pytest-xdist`
  (with `execnet`). Install them with `pip install -r requirements-dev.txt`.
  That file pins every package with sha256 hashes, so pip checks every
  download. They are not needed to run EQUIPA itself, only to test it.

## 3. Zero dependencies

EQUIPA depends only on the Python standard library. This is a hard constraint,
not a preference:

- **No third-party runtime imports.** No `requests`, no `pydantic`, no ORM.
  Use `urllib`, `sqlite3`, `json`, `ast`, `argparse`, `subprocess`, etc.
- **No `pip install` to run.** Copy the `equipa/` folder onto any machine with
  Python 3.10+ and it works. Zero supply-chain surface.
- The pytest packages in `requirements-dev.txt` are the single exception, and
  only for the test suite. Never import them from `equipa/` or `scripts/`
  production code.

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
| Tests | `.github/workflows/tests.yml` | Runs `python -m pytest -q -p no:cacheprovider -n auto --dist loadfile --durations=25` under Python 3.10 and 3.12 with a hermetic `THEFORGE_DB`. The full suite must pass. |
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
  default branch moved before those checks, nothing runs: the merge is
  aborted and the guard raises the alarm. The resolution commit is built
  with `git commit-tree` from the verified tree and the pinned parents, and
  the default branch is moved only by a compare-and-swap
  `git update-ref <branch> <resolution> <pinned SHA>`. If the branch moved
  after the checks (at any point up to that update), the swap is refused,
  nothing is committed on top of the moved branch, the branch is left where
  it was, and the guard raises the alarm. A branch that changed the
  generator ends `merge_failed` with "the task branch changed the
  generator"; it is never run. A generator
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
