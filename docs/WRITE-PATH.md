# The write path

## What this is about

Everything an agent says during a session is **conversation** — provisional, and
correctable in the next message. A small number of functions turn some of that
conversation into **documentation**: rows in `theforge.db` that later sessions
read and act on without re-deriving them.

The crossing between those two states is the **conversation-to-documentation
boundary**. It is a specific, small surface:

| Component | Role |
|---|---|
| `equipa_session_note_add` | session summaries, next steps, key points |
| `equipa_lesson_add` | lessons learned |
| `equipa_decision_add` | decisions, rationale, alternatives |
| `lesson_sanitizer.py` | the checks all three call |

> **Not `docs/`.** "Documentation" here means the durable record in the
> database — decisions, session notes, lessons. It has nothing to do with the
> markdown files in this directory, including this one.

Upstream of the boundary a mistake costs one message. Downstream it costs a
future session reading a record that is wrong and has no reason to doubt it.
That asymmetry is the whole reason this file exists.

## Two invariants

### 1. Store what was meant, or refuse

> Never store what is merely *storable*.

When input does not fit — too long, malformed, an unrecognised vocabulary value
— the tempting move is to salvage something writable and proceed. Truncate to
the cap. Keep the part that parsed. Fall back to the default. Every such
salvage produces a record that looks fine and is wrong, and it is written under
a `"status": "created"` that tells the caller everything went well.

Refuse instead, and say which field and why. A loud failure gets the call
retried correctly within seconds. A silent salvage is discovered weeks later, if
at all.

### 2. Nothing caller-controlled may be unbounded on a channel we don't drain

The MCP server logs to stderr, which is a pipe the *client* owns. If the client
does not drain it — and `subprocess.PIPE` does not, by default — the server has
only the pipe's buffer before a write blocks. Anything echoed into a log line
from caller-supplied data must therefore be capped, or a large-but-legal request
can wedge the server permanently.

This one is about liveness rather than fidelity, but it lives at the same
crossing and it failed the same way: quietly.

## What has actually gone wrong here

Four incidents, in order. Three are fidelity failures; the third is instructive
precisely because it is the one that failed *loudly*.

### 2026-06-18 — the 500-char cap gutted a session note

`sanitize_lesson_content()` hard-truncated all input to 500 characters, and
`/forge-end` piped session summaries through it. A 4,562-character hand-authored
note was cut to 500 and written. Caught by eye, restored by hand with a
direct `UPDATE` on `session_notes` id=136.

Two defects, not one: the truncation was silent, and a single cap was applied
across content types with wildly different natural lengths — a lesson wants ~500
characters, a session note wants thousands.

Task #100027 · fixed in `e85876b`, which split `sanitize()` (security, no cap)
from `enforce_limit()` (cap, logs a warning whenever it fires) and gave each
content type its own limit.

### 2026-07-08 — the `/forge-end` skill's SQL had drifted from the schema

The skill's templates referenced a `tasks.updated_at` column that did not exist,
omitted `decisions.topic` (which is `NOT NULL` with no default), and inserted
`open_questions.created_at` where the column is `asked_at`.

Fixed in `e5fe084`. **This is the contrasting case.** Every one of those errors
would have raised on execution — the insert simply fails — so the drift was
noticed, diagnosed and corrected in a single session, and left no wreckage
behind it. Compare the two incidents either side of it, which stored something
plausible and were found weeks and months later.

### 2026-08-01 to 2026-09-09 — tool-call framing swallowed into decision bodies

A malformed tool call can close a parameter with the *field's own name* —
`</decision>` where `</parameter>` was meant. Everything after that point,
including the framing of every parameter that follows, lands inside the first
field's value.

Nothing downstream noticed. The value is a well-formed string, so
injection-stripping passed, the length cap passed, the row was written, and the
tool returned `{"status": "created"}` with a plausible character count.

**44 decision records** across **3 projects** were stored this way over five and
a half weeks. Every single one had `rationale` NULL — the field the write tool
exists to capture — with the reasoning sitting inside the decision body instead.
Several also had `decision_type` silently defaulted to `general`, because the
intended value was inside the body too.

Repaired 2026-09-09 by redistributing the text back into its columns, verified
content-preserving on every row before applying. Guard added in MCP-07:
`find_tool_call_framing()` refuses any value containing `<parameter name=`,
checked on the raw arguments before sanitization.

### 2026-09-09 — a large payload deadlocked the server

`_handle_tools_call` logged the caller's arguments verbatim to stderr. Any tool
call carrying more arguments than the client's stderr pipe buffer wedged the
server permanently: the write to stderr blocked, the response was never written
to stdout, and the client waited forever. No error, no exit, no timeout.

Measured threshold on Windows: responds up to ~3,750 characters of arguments,
hangs above ~4,000. Confirmed as a pipe problem rather than a payload problem by
sending the identical request with stderr piped-and-undrained (hung) and with
stderr discarded (responded in 0.4s).

The buffer size is why it hid for so long — a platform with a larger pipe buffer
swallows the same payload and nothing shows. It is also why
`test_task_create_rejects_oversize_description` hung on Windows while CI stayed
green.

Fixed in MCP-08 by capping the logged arguments at `MAX_LOG_ARGS_CHARS = 500`
and reporting the true length, so the truncation is visible.

Worth noting against invariant 1: `equipa_session_note_add` advertises summaries
"up to ~50k chars", an order of magnitude past the deadlock threshold. The
documented capacity and the actual safe capacity had diverged, and nothing
connected them.

## What the boundary checks today

| Check | session_note_add | lesson_add | decision_add |
|---|:-:|:-:|:-:|
| Auth token | ✓ | ✓ | ✓ |
| Rate limit | ✓ | ✓ | ✓ |
| Project exists, status active/planning | ✓ | optional | ✓ |
| `EQUIPA_MCP_PROJECT_IDS` allowlist | ✓ | ✓ | ✓ |
| Injection / ANSI / base64 stripping | ✓ | ✓ | ✓ |
| Length cap, warning when it fires | ✓ | ✓ | ✓ |
| Required field non-empty after sanitizing | summary | lesson | topic + decision |
| Vocabulary allowlist | — | — | type + status |
| Tool-call framing refused (MCP-07) | ✓ | ✓ | ✓ |

## If you add a write tool

1. **Sanitize at the boundary, not at the caller.** The tool cannot assume its
   caller ran anything. That is the entire reason these tools exist rather than
   raw `write_query`.
2. **Check the raw arguments before sanitizing** for anything structural.
   `sanitize()` collapses runs of whitespace, which can reshape a marker you
   were about to match.
3. **Pick the length cap by measurement, not by symmetry.** The caps here are
   deliberately asymmetric — decision *bodies* accrete amendments and run far
   longer than rationales, so they use `sanitize_decision_body()`. Measure the
   real column before choosing a number.
4. **Refuse; do not repair.** A silent repair hides the caller's bug and leaves
   a record nobody knows to distrust.
5. **Name the field in the error.** "Something was malformed" costs a debugging
   session; "`rationale` contains tool-call framing" costs a retry.
6. **Cap anything caller-controlled that reaches a log line.** See invariant 2.

## Related

- `lesson_sanitizer.py` — the checks, each documented with the incident that
  prompted it
- `equipa/mcp_server.py` — the MCP-01..08 hardening list in the module docstring
- Task #100027 — the silent-truncation bug, the origin of the loud-failure rule
