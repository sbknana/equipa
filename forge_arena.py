#!/usr/bin/env python3
"""Forge Arena — Iterative Agent Training Loop for LoRA Data Generation.

Runs EQUIPA agents through repeated improvement cycles on a project,
generating high-quality training episodes for LoRA fine-tuning.

Each phase runs a full role rotation:
  developer → tester → security-reviewer → code-reviewer → developer (fixes)
Continues until all roles report clean, or max iterations per phase.

All episodes are automatically logged to TheForge agent_episodes table
by the orchestrator. After all phases complete, exports episodes as
LoRA-ready ChatML JSONL.

Usage:
    # Dry run — show phases and tasks without executing
    python3 forge_arena.py --dry-run

    # Run all phases for Apocrypha (default)
    python3 forge_arena.py

    # Run specific phase only
    python3 forge_arena.py --phase 1

    # Run with custom max iterations per phase
    python3 forge_arena.py --max-iterations 5

    # Export accumulated episodes as LoRA training data
    python3 forge_arena.py --export-lora

    # Resume from a specific phase (skip completed ones)
    python3 forge_arena.py --resume-from 3

    # Run BlockNet Go blockchain rewrite
    python3 forge_arena.py --project chain-node

    # Run EQUIPA Python rewrite
    python3 forge_arena.py --project equipa

Copyright 2026 Forgeborn
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Force unbuffered output
os.environ["PYTHONUNBUFFERED"] = "1"

# --- Config ---
SCRIPT_DIR = Path(__file__).parent
THEFORGE_DB = Path(os.environ.get("THEFORGE_DB", str(SCRIPT_DIR / "theforge.db")))
ORCHESTRATOR = SCRIPT_DIR / "forge_orchestrator.py"
EXPORT_DIR = SCRIPT_DIR / ".arena-exports"
LOG_DIR = SCRIPT_DIR / ".arena-logs"

# Project profiles — each project the Arena can target
PROJECT_PROFILES = {
    "apocrypha": {
        "id": 50,
        "codename": "apocrypha",
        "dir": "",
        "language": "go",
    },
    "chain-node": {
        "id": 19,
        "codename": "chain-node",
        "dir": "",
        "language": "go",
    },
    "equipa": {
        "id": 23,
        "codename": "equipa",
        "dir": "",
        "language": "python",
    },
    "babel": {
        "id": 51,
        "codename": "babel",
        "dir": "",
        "language": "go",
    },
}

# Default project
DEFAULT_PROJECT = "apocrypha"

# Active project config — set by --project flag or default
PROJECT_ID = PROJECT_PROFILES[DEFAULT_PROJECT]["id"]
PROJECT_CODENAME = PROJECT_PROFILES[DEFAULT_PROJECT]["codename"]
PROJECT_DIR = PROJECT_PROFILES[DEFAULT_PROJECT]["dir"]

# Arena settings
MAX_ITERATIONS_PER_PHASE = 4   # max dev→fix cycles before moving on
DISPATCH_TIMEOUT = 3600         # 60 min per agent dispatch
COOLDOWN_BETWEEN_TASKS = 10     # seconds between dispatches (be gentle)

# Role rotation for each phase iteration
ROLE_ROTATION = [
    "developer",
    "tester",
    "security-reviewer",
    "code-reviewer",
]


# --- Phase Definitions ---
# Each phase defines what the developer should build, and what other roles should check.
# Phases build on each other — Phase 2 assumes Phase 1 is complete, etc.

PHASES = [
    {
        "id": 1,
        "name": "Foundation — Config, DB Pool, Health Endpoint",
        "developer_task": """Build the Go API server foundation for Apocrypha.

**Requirements:**
1. Config loading via `envconfig` from environment variables:
   - DATABASE_URL, PORT, ANTHROPIC_API_KEY, DH_API_URL, DH_API_KEY, JWT_SECRET, FORGEBRIDGE_URL
   - Struct tags with sensible defaults (PORT=8080)
2. PostgreSQL connection pool using `pgxpool`:
   - MaxConns = (runtime.NumCPU() * 2) + 1
   - Health check enabled
   - Graceful shutdown on SIGINT/SIGTERM
3. Chi router with:
   - `/api/health` → 200 JSON `{"status":"ok","db":"connected","version":"0.1.0"}`
   - Structured JSON error responses for 404/500
   - Request ID middleware (X-Request-ID header)
   - CORS middleware for frontend
4. Graceful shutdown with context cancellation

**Files to create/modify:**
- `internal/config/config.go` — Config struct + Load()
- `internal/db/pool.go` — NewPool() with pgxpool
- `internal/api/router.go` — NewRouter() with health endpoint
- `internal/api/middleware.go` — RequestID, CORS, JSON error handler
- `cmd/server/main.go` — Wire everything, graceful shutdown

**Acceptance criteria:**
- `go build ./cmd/server` compiles with zero errors
- `go vet ./...` passes
- Health endpoint returns correct JSON
- Server shuts down cleanly on SIGINT
- No hardcoded config values — all from environment
""",
        "tester_task": """Write comprehensive tests for the Apocrypha Go API foundation.

**What to test:**
1. `internal/config/` — config loading from env vars, defaults, missing required vars
2. `internal/db/` — pool creation (mock or integration), health check
3. `internal/api/` — health endpoint returns 200 + correct JSON, 404 handler, request ID middleware
4. `cmd/server/` — graceful shutdown behavior

**Requirements:**
- Use `net/http/httptest` for HTTP tests
- Use `testing.T` with subtests (t.Run)
- Test both success and error paths
- Test missing DATABASE_URL returns error
- Test health endpoint with DB down returns degraded status
- Run `go test ./... -race` — all tests must pass with no data races

**Output:** List all test files created, test count, pass/fail results.
""",
        "security_task": """Security review the Apocrypha Go API foundation code.

**Focus areas:**
1. Config handling — are secrets logged? Exposed in health endpoint? Hardcoded anywhere?
2. CORS — is it overly permissive? Does it restrict origins properly?
3. Error responses — do they leak stack traces or internal details?
4. DB connection — is the pool properly bounded? Timeout configured?
5. Request ID — is it generated securely? Can it be spoofed via header injection?
6. Graceful shutdown — does it drain connections? Timeout on shutdown?
7. Dependencies — any known CVEs in go.mod deps?

**Output:** Severity-ranked findings with specific file:line references and fix recommendations.
""",
        "review_task": """Code review the Apocrypha Go API foundation.

**Check for:**
1. Go idioms — proper error handling (no `_` on errors), error wrapping with %w
2. Package structure — clean separation of concerns
3. Config — struct tags correct, validation present
4. Pool — proper lifecycle management, no leaks
5. Router — middleware ordering matters (RequestID before logging, etc.)
6. No lazy patterns: no `panic()` in handlers, no `log.Fatal` after startup
7. Proper context propagation through request chain
8. Comments where non-obvious, but no over-commenting

**Output:** Specific issues with file:line, severity (must-fix, should-fix, nit), and suggested fixes.
""",
    },
    {
        "id": 2,
        "name": "Database Schema + sqlc Code Generation",
        "developer_task": """Set up database schema and sqlc code generation for Apocrypha.

**Requirements:**
1. Apply the migration in `migrations/001_schema.sql` (the full schema is already written)
2. Configure sqlc (`sqlc.yaml`) to generate Go code from SQL queries
3. Write sqlc query files for core CRUD operations:

**Queries to implement (in `internal/db/queries/`):**
- `worlds.sql`: CreateWorld, GetWorld, ListWorlds
- `civilizations.sql`: CreateCiv, GetCiv, ListCivsByWorld, UpdateCivStatus
- `artifacts.sql`: CreateArtifact, GetArtifact, ListArtifacts (with pagination), SearchArtifacts (full-text), ListArtifactsByCiv, GetArtifactByCatalogNumber
- `scholars.sql`: CreateScholar, GetScholar, ListScholars
- `controversies.sql`: CreateControversy, GetControversy, ListControversies, UpdateVoteCount
- `museum_wings.sql`: CreateWing, GetWing, ListWings, AddArtifactToWing
- `civ_relations.sql`: CreateRelation, GetRelationsByCiv
- `generation_log.sql`: CreateLogEntry, GetCostSummary
- `user_profiles.sql`: UpsertProfile, GetProfile

4. Run `sqlc generate` — must produce valid Go code
5. Ensure all queries use parameterized inputs (no string concatenation)

**Acceptance criteria:**
- `sqlc generate` succeeds with zero errors
- `sqlc vet` passes
- Generated Go code compiles: `go build ./...`
- Full-text search query uses `to_tsvector`/`plainto_tsquery` (not LIKE)
- Pagination uses LIMIT/OFFSET with proper parameter binding
""",
        "tester_task": """Write integration tests for the Apocrypha database layer.

**What to test:**
1. Schema migration applies cleanly to a fresh database
2. All sqlc-generated CRUD operations work correctly:
   - Create → Get returns same data
   - List with pagination returns correct pages
   - Search returns matching results
   - Update modifies correct fields
3. Constraint enforcement:
   - era_start < era_end (CHECK constraint)
   - Unique catalog_number
   - Foreign key relationships (artifact → civilization → world)
   - no_self_relation on civ_relations
4. Full-text search actually finds artifacts by name/description
5. JSONB fields store and retrieve correctly (geography, religion, etc.)

**Requirements:**
- Use a test database (create/drop per test suite)
- Use `pgxpool` for connections (same as production)
- Test concurrent operations for race conditions
- `go test ./... -race` must pass

**Output:** Test count, pass/fail, coverage percentage.
""",
        "security_task": """Security review the Apocrypha database layer and sqlc queries.

**Focus areas:**
1. SQL injection — are ALL queries parameterized? Any string interpolation?
2. Full-text search — is the search query safe from injection via tsquery syntax?
3. JSONB handling — can malformed JSON crash queries?
4. Privilege escalation — can a user modify another user's data through CRUD operations?
5. Data exposure — do any queries return more fields than needed?
6. Migration safety — are there any destructive operations without IF EXISTS guards?
7. Connection string — is DATABASE_URL handled safely?
8. Batch operations — any unbounded queries that could DoS the database?

**Output:** Severity-ranked findings with file:line references.
""",
        "review_task": """Code review the Apocrypha database layer.

**Check for:**
1. sqlc query organization — logical grouping, consistent naming
2. Query efficiency — proper indexing used, no N+1 patterns, EXPLAIN-friendly
3. Pagination — uses keyset pagination or LIMIT/OFFSET correctly
4. Error handling in generated code — proper pgx error type checking
5. Null handling — proper use of pgtype for nullable fields
6. Transaction usage where needed (e.g., creating world + civilizations)
7. Connection pool usage — no connection leaks, proper defer patterns
8. Migration idempotency — can it be run multiple times safely?

**Output:** Issues with severity and suggested improvements.
""",
    },
    {
        "id": 3,
        "name": "REST API Endpoints — Full CRUD",
        "developer_task": """Build all REST API endpoints for Apocrypha using Chi router and sqlc-generated DB code.

**Endpoints to implement:**

Worlds:
- GET    /api/worlds           → list all worlds (paginated)
- GET    /api/worlds/:id       → get world by ID
- POST   /api/worlds           → create world (admin only, later)

Civilizations:
- GET    /api/civilizations              → list all civilizations (filterable by status, world_id)
- GET    /api/civilizations/:id          → get civilization detail
- GET    /api/civilizations/:id/artifacts → list artifacts for civilization
- GET    /api/civilizations/:id/relations → list relations for civilization

Artifacts:
- GET    /api/artifacts                  → list artifacts (paginated, filterable by type, rarity, civ)
- GET    /api/artifacts/:id              → get artifact detail
- GET    /api/artifacts/search?q=term    → full-text search

Scholars:
- GET    /api/scholars                   → list all scholars
- GET    /api/scholars/:id               → get scholar with linked controversies

Controversies:
- GET    /api/controversies              → list all controversies
- GET    /api/controversies/:id          → get controversy detail with evidence

Museum Wings:
- GET    /api/wings                      → list museum wings with artifact counts
- GET    /api/wings/:id                  → get wing with artifacts

**Implementation requirements:**
- Use Chi URL params: `chi.URLParam(r, "id")`
- Parse UUID params safely — return 400 on invalid UUID
- JSON response helpers: `respondJSON(w, status, data)` and `respondError(w, status, msg)`
- Pagination: `?page=1&per_page=20` with sensible defaults and max limits
- Filter params: `?status=active&type=pottery&rarity=rare`
- Proper HTTP status codes: 200, 201, 400, 404, 500
- All handlers get `*pgxpool.Pool` via closure or dependency injection

**Files:**
- `internal/api/worlds.go`
- `internal/api/civilizations.go`
- `internal/api/artifacts.go`
- `internal/api/scholars.go`
- `internal/api/controversies.go`
- `internal/api/wings.go`
- `internal/api/helpers.go` — respondJSON, respondError, parsePagination, parseUUID
- Update `internal/api/router.go` to mount all routes

**Acceptance criteria:**
- `go build ./...` compiles
- `go vet ./...` passes
- Every endpoint returns proper JSON with correct status codes
- Invalid UUIDs return 400, missing resources return 404
- Pagination works with default and custom values
""",
        "tester_task": """Write HTTP integration tests for all Apocrypha REST API endpoints.

**Test every endpoint:**
1. Worlds: GET list (empty + populated), GET by ID (found + not found)
2. Civilizations: GET list, GET with filters (?status=active), GET by ID, GET artifacts, GET relations
3. Artifacts: GET list, GET with pagination, GET by ID, GET search (matching + no results)
4. Scholars: GET list, GET by ID
5. Controversies: GET list, GET by ID with evidence
6. Wings: GET list with counts, GET by ID with artifacts

**Test error cases:**
- Invalid UUID → 400
- Non-existent ID → 404
- Invalid pagination params → defaults used
- Empty search query → 400

**Requirements:**
- Use `httptest.NewServer` with full router (integration tests)
- Seed test data in a test database
- Test response JSON structure matches expected schema
- Test pagination: first page, last page, out of range
- Test filter combinations
- `go test ./... -race -count=1` must pass

**Output:** Test count per endpoint, pass/fail, any flaky tests.
""",
        "security_task": """Security review the Apocrypha REST API endpoints.

**Focus areas:**
1. Input validation — are all URL params validated? Query params sanitized?
2. SQL injection via filter params — are ?status=, ?type=, etc. parameterized or whitelisted?
3. Pagination abuse — can someone set per_page=999999? Is there a max?
4. Path traversal — can manipulated IDs access wrong resources?
5. Information disclosure — do error messages reveal internal details?
6. IDOR — can a user access resources they shouldn't?
7. Rate limiting readiness — are endpoints structured for easy rate limiting?
8. Response size limits — can a crafted request return excessive data?
9. Search injection — can full-text search query be used for DoS?
10. CORS headers — present and correct on all responses?

**Output:** OWASP-categorized findings with severity ratings and fix code.
""",
        "review_task": """Code review all Apocrypha REST API handlers.

**Check for:**
1. Handler consistency — same patterns used across all endpoints
2. Error handling — all error paths return proper JSON (not plain text)
3. UUID parsing — consistent, no panics on invalid input
4. Query parameter handling — defaults, validation, whitelist for enum values
5. Response structure — consistent envelope (data, meta, error)
6. Pagination — consistent implementation, total count included
7. No business logic in handlers — handlers should only parse input → call DB → format output
8. Context propagation — request context passed through to DB calls
9. Logging — structured logging for errors, not for successful requests
10. Test coverage — are all code paths tested?

**Output:** Actionable issues with file:line, categorized by severity.
""",
    },
    {
        "id": 4,
        "name": "JWT Auth System + Protected Routes",
        "developer_task": """Implement JWT authentication for the Apocrypha API.

**Requirements:**
1. Auth endpoints:
   - POST /api/auth/register — create user profile, issue JWT pair
   - POST /api/auth/login — magic link stub (for now, accept email + return JWT)
   - POST /api/auth/refresh — refresh access token using refresh token
   - POST /api/auth/logout — invalidate refresh token

2. JWT implementation:
   - Access token: HS256, 15-minute expiry, claims: {sub: email, role: "visitor", exp, iat}
   - Refresh token: HS256, 7-day expiry, stored in httpOnly cookie
   - JWT secret from config (JWT_SECRET env var)

3. Auth middleware:
   - `RequireAuth` — validates access token, sets user in context
   - `OptionalAuth` — sets user if token present, continues if not
   - User context helpers: `GetUser(ctx)`, `GetUserEmail(ctx)`

4. Protected routes:
   - POST /api/votes — require auth
   - POST /api/commissions — require auth
   - GET /api/profile — require auth
   - All GET /api/* — optional auth (for personalization)

5. Rate limiting (go-chi/httprate):
   - Auth endpoints: 10/min per IP
   - Protected POST endpoints: per-user limits from plan
   - GET endpoints: 200/hour per user or IP

**Files:**
- `internal/auth/jwt.go` — token generation, validation, claims
- `internal/auth/middleware.go` — RequireAuth, OptionalAuth, context helpers
- `internal/api/auth.go` — auth endpoint handlers
- `internal/api/votes.go` — vote endpoint (POST /api/controversies/:id/vote)
- Update router.go with auth middleware on appropriate routes

**Acceptance criteria:**
- `go build ./...` compiles
- JWT tokens are properly signed and validated
- Expired tokens return 401
- Refresh flow works correctly
- Protected routes reject unauthenticated requests
- Rate limits actually enforce (not just headers)
""",
        "tester_task": """Write comprehensive tests for the Apocrypha JWT auth system.

**Test cases:**
1. Registration:
   - Valid registration returns JWT pair
   - Duplicate email returns 409
   - Invalid email returns 400

2. Login:
   - Valid email returns JWT pair
   - Creates user profile if doesn't exist

3. Token validation:
   - Valid access token passes middleware
   - Expired token returns 401
   - Malformed token returns 401
   - Token signed with wrong secret returns 401
   - Missing Authorization header returns 401

4. Refresh flow:
   - Valid refresh token returns new access token
   - Expired refresh token returns 401
   - Refresh token cannot be used as access token

5. Protected routes:
   - POST /api/votes without auth → 401
   - POST /api/votes with valid auth → processes request
   - GET /api/artifacts with no auth → still works (optional auth)

6. Rate limiting:
   - Exceed auth rate limit → 429
   - Rate limit headers present (X-RateLimit-*)

7. Security edge cases:
   - JWT with `alg: none` → rejected
   - JWT with different algorithm → rejected
   - Token in query string → ignored (only Authorization header)

**Run `go test ./... -race` — all must pass.**
""",
        "security_task": """Security review the Apocrypha JWT authentication system.

**CRITICAL checks:**
1. JWT implementation:
   - Is `alg: none` attack prevented?
   - Is algorithm explicitly validated (not trusting header)?
   - Is the secret sufficiently entropic? Minimum length enforced?
   - Are claims properly validated (exp, iat, sub)?
2. Token storage:
   - Refresh token in httpOnly, Secure, SameSite=Strict cookie?
   - No tokens in URL, localStorage recommendations, etc.?
3. Rate limiting:
   - Is it actually sliding window, not fixed?
   - Can it be bypassed via X-Forwarded-For spoofing?
   - Is IP extraction secure (trusted proxy)?
4. Session management:
   - Can refresh tokens be revoked?
   - Is there a token blacklist for logout?
5. Registration/Login:
   - Email enumeration via timing or response differences?
   - Account lockout after failed attempts?
6. Middleware:
   - Does RequireAuth actually block or just warn?
   - Can middleware be bypassed via route ordering?
7. CSRF:
   - Are state-changing requests protected against CSRF?
   - SameSite cookie attribute set?

**Output:** OWASP A07 (Auth Failures) focused review with severity ratings.
""",
        "review_task": """Code review the Apocrypha JWT auth system.

**Check for:**
1. JWT library usage — proper claims struct, no custom crypto
2. Middleware composition — correct ordering (rate limit before auth)
3. Error handling — auth failures don't leak information
4. Context propagation — user claims available downstream
5. Token generation — proper randomness, no predictable tokens
6. Rate limiter configuration — matches plan requirements
7. Handler structure — auth handlers follow same patterns as CRUD handlers
8. Logout implementation — actually invalidates tokens
9. Cookie settings — all security flags set
10. No hardcoded secrets or test tokens in code

**Output:** Issues with severity, fix suggestions, and Go best practices.
""",
    },
    {
        "id": 5,
        "name": "World Consistency Engine + Constraint Graph",
        "developer_task": """Implement the World Consistency Engine — the core differentiator of Apocrypha.

**Requirements:**
1. Constraint Graph (in-memory, loaded from DB on startup):
   ```go
   type ConstraintGraph struct {
       mu       sync.RWMutex
       adjList  map[uuid.UUID][]Edge
       civIndex map[uuid.UUID]*CivNode
   }
   ```
   - Load from civ_relations table on startup
   - Thread-safe reads via RWMutex
   - Update on new relation creation (atomic with DB transaction)

2. Context Builder:
   - BFS traversal up to configurable depth (default 2)
   - Collects all constraints from related civilizations
   - Returns `GenerationContext` with primary civ, constraints, related civs
   - O(V + E) complexity within subgraph — no full graph traversal

3. Post-Generation Validator:
   - Era bounds check (artifact era within civilization era range)
   - Material plausibility (bronze before iron age, etc.)
   - Visual language consistency (motifs match civilization style)
   - Returns []Violation with field + message
   - Max 2 retry prompts on violation, then flag for review

4. Technology level definitions:
   - Stone Age: stone, bone, wood, clay, leather
   - Bronze Age: + bronze, copper, tin, gold, silver
   - Iron Age: + iron, steel, glass
   - Define as a map[string][]string for easy lookup

**Files:**
- `internal/engine/graph.go` — ConstraintGraph, Edge, CivNode
- `internal/engine/context.go` — BuildContext(), GenerationContext
- `internal/engine/validator.go` — ValidateArtifact(), Violation
- `internal/engine/tech_levels.go` — technology/material mappings
- `internal/engine/engine.go` — Engine struct wiring graph + validator

**Acceptance criteria:**
- `go build ./...` compiles
- `go vet ./...` passes
- BFS traversal works correctly (unit tests with known graph)
- Validator catches era violations, material violations
- Graph handles concurrent reads safely
- No panics on empty graph or disconnected nodes
""",
        "tester_task": """Write thorough tests for the Apocrypha World Consistency Engine.

**Test the Constraint Graph:**
1. Load from empty DB → empty graph, no errors
2. Load with 3 civs, 4 relations → correct adjacency list
3. Concurrent reads don't race (use -race flag)
4. Update graph while reading doesn't deadlock

**Test the Context Builder (BFS):**
1. Single civ with no relations → context has only primary civ
2. Linear chain A→B→C, depth=1 → only A and B
3. Linear chain A→B→C, depth=2 → all three
4. Cyclic graph → doesn't loop infinitely
5. Disconnected nodes → only reachable nodes included
6. Large graph (100 nodes) → completes in reasonable time

**Test the Validator:**
1. Artifact era within civ range → no violations
2. Artifact era before civ start → violation
3. Artifact era after civ end → violation
4. Bronze material in stone age civ → violation
5. Stone material in iron age civ → no violation (backward compatible)
6. Multiple violations on same artifact → all reported
7. Null/empty fields → handled gracefully

**Test Technology Levels:**
1. Each era includes materials from previous eras
2. Unknown material → flagged
3. Lookup is O(1) or O(materials)

**`go test ./internal/engine/... -race -v` must all pass.**
""",
        "security_task": """Security review the Apocrypha World Consistency Engine.

**Focus areas:**
1. Memory safety — can the graph grow unbounded? Is there a max size?
2. Concurrency — is RWMutex used correctly? Any potential deadlocks?
3. DoS via graph operations — can a request trigger expensive BFS on full graph?
4. Input validation — are UUIDs validated before graph lookup?
5. Cycle detection — does BFS handle cycles safely?
6. Resource limits — is BFS depth bounded? Max nodes visited?
7. Validator bypass — can constraints be circumvented by crafting input?
8. Race conditions — is graph update atomic with DB transaction?
9. Error handling — do graph errors crash the server?
10. Cache invalidation — is stale graph data a security concern?

**Output:** Findings with severity and remediation recommendations.
""",
        "review_task": """Code review the Apocrypha World Consistency Engine.

**Check for:**
1. Data structure choices — is adjacency list optimal? Consider alternatives
2. BFS implementation — correct, efficient, no unnecessary allocations
3. Mutex usage — RLock for reads, Lock for writes, no lock held during I/O
4. Graph loading — batched query, not N+1
5. Validator design — extensible for new constraint types
6. Error handling — all paths covered, meaningful error messages
7. Testing — edge cases covered, no flaky tests
8. Memory usage — graph size estimation for expected data volumes
9. API design — clean public interface, implementation details hidden
10. Documentation — complex algorithms well-commented

**Output:** Architecture feedback + specific code issues.
""",
    },
]


# --- BlockNet Go Blockchain Rewrite Phases ---
# Complete rewrite from Node.js → Go. CometBFT consensus, pgx state, 2500 TPS / 500ms blocks.

BLOCK_NET_PHASES = [
    {
        "id": 1,
        "name": "Foundation — Go Module, Config, CometBFT ABCI Scaffold",
        "developer_task": """Build the Go foundation for the BlockNet blockchain.

**Context:** This is a complete rewrite of the Node.js BlockNet blockchain in Go.
Target: 2500 TPS, 500ms block time, PoH+PoS consensus via CometBFT ABCI.

**Requirements:**
1. Go module: `github.com/forgeborn/chain-node`
2. Config via `envconfig`:
   - CHAIN_ID, MONIKER, GENESIS_PATH, DB_PATH, RPC_PORT (26657), P2P_PORT (26656)
   - VALIDATOR_KEY_PATH, LOG_LEVEL, MAX_VALIDATORS
3. CometBFT ABCI v2 application scaffold:
   - Implement `abci.Application` interface (CheckTx, FinalizeBlock, Commit, Query, Info)
   - Wire CometBFT node with the ABCI app
   - Genesis loading from JSON file
4. State storage using BadgerDB (or bbolt) for chain state:
   - Account balances, nonces, validator set
   - State root hash computation (Merkle tree)
5. Logging via `slog` (Go 1.22+ structured logging)
6. Graceful shutdown on SIGINT/SIGTERM

**Files:**
- `cmd/habeas/main.go` — Entry point, wire CometBFT + ABCI app
- `internal/config/config.go` — Config struct + Load()
- `internal/app/app.go` — ABCI Application (CheckTx, FinalizeBlock, Commit, Query)
- `internal/state/store.go` — State store interface + BadgerDB impl
- `internal/state/accounts.go` — Account model (balance, nonce, staked)
- `internal/types/genesis.go` — Genesis struct, load + validate
- `go.mod`, `go.sum`

**Acceptance criteria:**
- `go build ./cmd/habeas` compiles
- `go vet ./...` passes
- CometBFT node starts with empty genesis
- ABCI Info returns chain version
- State store opens/closes cleanly
""",
        "tester_task": """Write tests for the BlockNet Go blockchain foundation.

**Test:**
1. Config loading — defaults, env override, missing required vars error
2. Genesis parsing — valid genesis, malformed JSON, missing fields
3. State store — open, write account, read account, close, reopen preserves data
4. Account operations — credit, debit, insufficient balance error, nonce increment
5. ABCI Info — returns correct chain ID and version
6. ABCI CheckTx — valid tx format accepted, invalid rejected
7. Graceful shutdown — node stops cleanly

**Requirements:**
- `go test ./... -race` must pass
- Use `testing.T` with subtests
- Temp directories for BadgerDB in tests (cleanup after)
""",
        "security_task": """Security review the BlockNet blockchain foundation.

**Focus:**
1. Key management — validator keys loaded safely? Not logged?
2. Genesis validation — can malicious genesis crash node?
3. Config — secrets exposed in logs or errors?
4. State store — can concurrent access corrupt state?
5. ABCI interface — can malformed requests from CometBFT crash app?
6. Dependencies — known CVEs in CometBFT, BadgerDB?
7. Memory — unbounded allocations on startup?
""",
        "review_task": """Code review the BlockNet blockchain foundation.

**Check:**
1. Go idioms — error wrapping, no panic in production paths
2. CometBFT ABCI v2 interface — correctly implemented
3. State store abstraction — clean interface, impl swappable
4. Config struct — proper validation, no hardcoded values
5. Module structure — clean separation (app, state, types, config)
6. Logging — structured, appropriate levels
""",
    },
    {
        "id": 2,
        "name": "Transaction Engine — Types, Validation, Mempool",
        "developer_task": """Implement the transaction engine for BlockNet.

**Transaction types:**
1. Transfer: {from, to, amount, fee, nonce, signature}
2. Stake: {validator, amount, delegator, nonce, signature}
3. Unstake: {validator, amount, delegator, nonce, signature}
4. Governance vote: {proposal_id, vote, voter, nonce, signature}

**Requirements:**
1. Transaction struct with type discriminator + RLP/Protobuf encoding
2. Signature verification using secp256k1 (Go crypto/ecdsa or btcec)
3. Transaction validation:
   - Signature valid
   - Nonce matches account nonce + 1
   - Balance >= amount + fee
   - Fee >= minimum fee (configurable)
4. Mempool:
   - Priority queue ordered by fee (highest first)
   - Max size configurable (default 10000 txs)
   - Nonce gap detection — don't accept nonce 5 if 3 hasn't been seen
   - Duplicate detection (tx hash)
   - Eviction of lowest-fee txs when full
5. Wire into ABCI CheckTx (validate) and FinalizeBlock (execute)

**Files:**
- `internal/types/tx.go` — Transaction types, encoding, hash
- `internal/crypto/keys.go` — Key generation, signing, verification (secp256k1)
- `internal/crypto/address.go` — Address derivation from public key
- `internal/mempool/mempool.go` — Priority queue mempool
- `internal/app/execute.go` — Transaction execution (state transitions)
- Update `internal/app/app.go` — Wire CheckTx + FinalizeBlock

**Acceptance criteria:**
- `go build ./...` compiles
- Transfer tx: signed, validated, executed, balances updated
- Invalid signature → rejected at CheckTx
- Wrong nonce → rejected
- Insufficient balance → rejected
- Mempool respects size limit and fee ordering
""",
        "tester_task": """Write comprehensive tests for the BlockNet transaction engine.

**Test:**
1. Transaction encoding/decoding roundtrip
2. Signature generation + verification (valid, invalid, wrong key)
3. Address derivation determinism
4. All 4 tx types validate correctly
5. Nonce enforcement (correct, too low, too high, gap)
6. Balance checks (exact, insufficient, overflow protection)
7. Fee validation (below minimum, exact minimum, above)
8. Mempool ordering by fee
9. Mempool eviction when full
10. Mempool duplicate rejection
11. End-to-end: sign tx → CheckTx → FinalizeBlock → state updated
12. Concurrent mempool access (race detector)

**`go test ./... -race -count=3` must pass consistently.**
""",
        "security_task": """Security review the BlockNet transaction engine.

**CRITICAL checks:**
1. Signature verification — replay attack prevention (chain_id in signed data?)
2. Integer overflow — can amount + fee overflow uint64? Use checked arithmetic?
3. Nonce — can nonce be manipulated to skip or replay transactions?
4. Mempool DoS — can an attacker flood with min-fee txs? Size limits?
5. Transaction malleability — is tx hash computed over all fields including signature?
6. Key generation — proper entropy source? No weak RNG?
7. secp256k1 — using battle-tested library (btcec), not custom implementation?
8. Fee market manipulation — can txs be crafted to game the priority queue?
9. State transitions — are they atomic? Can partial execution corrupt state?
""",
        "review_task": """Code review the BlockNet transaction engine.

**Check:**
1. Encoding format choice — protobuf vs RLP vs custom. Is it deterministic?
2. Crypto library choice — btcec (Bitcoin's) vs stdlib ecdsa
3. Mempool data structure — heap vs sorted slice, performance at 10K txs
4. Transaction execution — proper state rollback on error?
5. Error types — domain-specific errors, not generic
6. Testing — edge cases (zero amount, max uint64, self-transfer)
""",
    },
    {
        "id": 3,
        "name": "Proof-of-History + Proof-of-Stake Consensus",
        "developer_task": """Implement PoH+PoS consensus for BlockNet.

**Proof-of-History (PoH):**
1. Sequential SHA-256 hash chain for verifiable passage of time
2. PoH generator goroutine running continuously:
   - `hash[n] = SHA256(hash[n-1] || counter)`
   - Records checkpoints every N hashes
3. Transaction injection into PoH stream (records ordering proof)
4. PoH verification: given start hash, count, and end hash — verifiable in O(count)
5. Target: verifier can check 500ms of PoH in < 100ms

**Proof-of-Stake:**
1. Validator set management:
   - Minimum stake: 10,000 HABEAS
   - Max validators: 100 (configurable)
   - Validator selection weighted by stake
2. Block proposer selection:
   - Round-robin weighted by stake + PoH slot assignment
   - Leader schedule generated per epoch (1000 blocks)
3. Slashing conditions:
   - Double-signing (equivocation) → 5% stake slash
   - Downtime (missed 100 consecutive blocks) → 1% slash
   - Evidence submission via special tx
4. Epoch transitions:
   - Recalculate validator set
   - Distribute block rewards (10 HABEAS/block to proposer)
   - Process pending stake/unstake

**Files:**
- `internal/poh/generator.go` — PoH hash chain generator
- `internal/poh/verifier.go` — PoH verification
- `internal/consensus/validators.go` — Validator set, selection, rotation
- `internal/consensus/staking.go` — Stake, unstake, slashing
- `internal/consensus/epoch.go` — Epoch transitions, reward distribution
- `internal/consensus/leader.go` — Leader schedule generation
- Update ABCI app to use PoH ordering + validator set

**Acceptance criteria:**
- PoH generates verifiable hash chains
- Validator selection is deterministic + weighted
- Slashing reduces stake correctly
- Epoch transitions update validator set
- Block rewards distributed to proposer
- `go build ./...` + `go vet ./...` pass
""",
        "tester_task": """Write tests for BlockNet PoH+PoS consensus.

**Test PoH:**
1. Hash chain generation determinism (same input → same output)
2. Verification of valid chain → passes
3. Verification of tampered chain → fails
4. Transaction injection maintains chain integrity
5. Performance: 500ms of PoH verifiable in < 100ms (benchmark test)

**Test PoS:**
1. Validator registration (above/below minimum stake)
2. Weighted selection distribution (Monte Carlo: 10K selections match stake ratio)
3. Leader schedule determinism
4. Double-sign slashing (correct amount, evidence valid/invalid)
5. Downtime slashing (missed block counting)
6. Epoch transition (validator set changes, rewards distributed)
7. Unstaking delay (unbonding period)
8. Edge cases: single validator, all validators equal stake, max validators

**`go test ./... -race -bench=. -benchtime=3s` for perf + correctness.**
""",
        "security_task": """Security review BlockNet consensus layer.

**CRITICAL:**
1. PoH — can the hash chain be forked or pre-computed to manipulate ordering?
2. Validator selection — is the RNG seed manipulable by validators?
3. Slashing — can an attacker trigger false slashing evidence?
4. Stake accounting — integer overflow on large stakes?
5. Epoch transitions — race condition between block production and epoch change?
6. Leader schedule — can next leader be predicted + targeted for DoS?
7. Nothing-at-stake — does the design prevent validators from voting on multiple forks?
8. Long-range attacks — any protection against rewriting old history?
9. Block reward — can rewards be claimed twice?
""",
        "review_task": """Code review BlockNet consensus implementation.

**Check:**
1. PoH generator — goroutine lifecycle, clean shutdown, no goroutine leaks
2. Hash function — using crypto/sha256 (not md5 or custom)
3. Validator set — efficient data structure for weighted selection
4. Slashing math — checked arithmetic, no precision loss
5. Epoch logic — clean state transitions, no partial updates
6. Concurrency — PoH generator vs block production synchronization
7. Test quality — consensus code MUST have thorough tests
""",
    },
    {
        "id": 4,
        "name": "P2P Network + Block Propagation",
        "developer_task": """Implement P2P networking for BlockNet using libp2p.

**Requirements:**
1. libp2p host with:
   - TCP + QUIC transports
   - Noise protocol for encryption
   - mDNS for local peer discovery
   - Kademlia DHT for internet peer discovery
   - GossipSub for block/tx propagation
2. Message types (protobuf-encoded):
   - NewBlock — propagate new blocks
   - NewTx — propagate transactions to mempool
   - BlockRequest/BlockResponse — sync missing blocks
   - StatusRequest/StatusResponse — chain height + head hash
3. Block sync:
   - On connect: exchange status, identify chain tip
   - If behind: request missing blocks in batches (100 at a time)
   - Validate blocks during sync (signatures, state transitions)
4. Peer management:
   - Max peers: 50 (configurable)
   - Peer scoring (penalize bad blocks, slow responses)
   - Bootstrap nodes list in config
5. Metrics:
   - Connected peers count
   - Blocks/txs propagated per second
   - Sync progress percentage

**Files:**
- `internal/p2p/host.go` — libp2p host setup, transports, discovery
- `internal/p2p/gossip.go` — GossipSub topics for blocks + txs
- `internal/p2p/sync.go` — Block sync protocol
- `internal/p2p/messages.go` — Protobuf message definitions
- `internal/p2p/peers.go` — Peer scoring + management
- `proto/p2p.proto` — Protobuf definitions

**Acceptance criteria:**
- Two nodes discover each other via mDNS
- Transaction gossip: submit tx to node A → appears in node B mempool
- Block gossip: node A produces block → node B receives + validates
- Chain sync: node C joins late → syncs to head
- `go build ./...` compiles with protobuf codegen
""",
        "tester_task": """Write tests for BlockNet P2P networking.

**Test:**
1. Host creation + teardown (no goroutine leaks)
2. Peer discovery via mDNS (2 nodes on loopback)
3. GossipSub message delivery (send → receive on subscriber)
4. Block propagation (create block → gossip → received by peer)
5. Transaction propagation
6. Block sync — node joins late, syncs 100 blocks
7. Invalid block rejection (bad signature → peer penalized)
8. Max peers enforcement
9. Concurrent connections (10 nodes mesh)
10. Protobuf encode/decode roundtrip for all message types

**Use `testing.Short()` to skip slow network tests in CI.**
**`go test ./... -race` must pass.**
""",
        "security_task": """Security review BlockNet P2P layer.

**Focus:**
1. Eclipse attack — can attacker monopolize all peer slots?
2. Block flooding — rate limiting on incoming blocks?
3. Transaction spam — mempool DoS via P2P?
4. Sybil attack — peer scoring + connection limits sufficient?
5. Network partition — graceful handling?
6. Protobuf parsing — can malformed messages crash the node?
7. GossipSub — message deduplication, amplification attacks?
8. Sync protocol — can a malicious peer serve invalid blocks during sync?
9. Encryption — Noise protocol correctly configured?
10. Bootstrap nodes — what if all bootstrap nodes are compromised?
""",
        "review_task": """Code review BlockNet P2P networking.

**Check:**
1. libp2p usage — idiomatic, no deprecated APIs
2. Goroutine management — all background goroutines tracked + stopped
3. Context propagation — all network ops use context with timeout
4. Error handling — network errors logged, not panicked
5. Message validation — validate before processing, not after
6. Protobuf — generated code, not hand-written serialization
7. Resource cleanup — hosts, streams, connections properly closed
8. Test isolation — tests don't conflict on ports
""",
    },
    {
        "id": 5,
        "name": "RPC API + CLI + State Channel Foundation",
        "developer_task": """Implement the RPC API and CLI for BlockNet.

**JSON-RPC 2.0 API (served alongside CometBFT RPC):**
1. Chain queries:
   - `habeas_chainInfo` — chain ID, height, head hash, peers
   - `habeas_getBlock` — block by height or hash
   - `habeas_getTransaction` — tx by hash with receipt
   - `habeas_getAccount` — balance, nonce, stake
2. Transaction submission:
   - `habeas_sendTransaction` — submit signed tx, return hash
   - `habeas_estimateFee` — estimate fee for tx type
3. Validator queries:
   - `habeas_getValidatorSet` — current validators with stake
   - `habeas_getEpochInfo` — current epoch, rewards, schedule
4. Subscription (WebSocket):
   - `habeas_subscribe("newBlocks")` — stream new blocks
   - `habeas_subscribe("pendingTxs")` — stream new mempool txs

**CLI (`habeas` binary — same binary, subcommands):**
- `habeas node start` — start validator/full node
- `habeas tx transfer --to <addr> --amount <n>` — send transfer
- `habeas tx stake --validator <addr> --amount <n>` — stake
- `habeas query account <addr>` — check balance
- `habeas query block <height>` — get block
- `habeas keys generate` — create keypair
- `habeas keys list` — show addresses

**State Channels (foundation only):**
- Open channel: lock funds on-chain, exchange signed states off-chain
- Close channel: submit final state on-chain, challenge period (100 blocks)
- Fraud proof: submit earlier signed state to penalize cheater
- This is the foundation for DragonHoard and Loom integration

**Files:**
- `internal/rpc/server.go` — JSON-RPC server setup
- `internal/rpc/handlers.go` — All RPC method implementations
- `internal/rpc/ws.go` — WebSocket subscription handler
- `internal/channels/channel.go` — State channel types + operations
- `internal/channels/manager.go` — Channel lifecycle management
- `cmd/habeas/cmd_node.go` — Node start command
- `cmd/habeas/cmd_tx.go` — Transaction commands
- `cmd/habeas/cmd_query.go` — Query commands
- `cmd/habeas/cmd_keys.go` — Key management commands

**Acceptance criteria:**
- RPC server responds to all defined methods
- CLI commands work end-to-end against running node
- WebSocket subscriptions deliver events
- State channel open + close works with valid signatures
- `go build ./cmd/habeas` produces single binary with all commands
""",
        "tester_task": """Write tests for BlockNet RPC, CLI, and state channels.

**RPC tests:**
1. All query methods return correct data
2. sendTransaction with valid tx → accepted
3. sendTransaction with invalid tx → error with reason
4. Subscription delivers blocks in real-time
5. Concurrent RPC requests (100 parallel) → all succeed

**CLI tests:**
1. `keys generate` creates valid keypair
2. `tx transfer` signs and submits correctly
3. `query account` returns formatted output
4. Invalid flags → helpful error messages

**State channel tests:**
1. Open channel → funds locked on-chain
2. Off-chain state updates with valid signatures
3. Cooperative close → funds distributed
4. Dispute close → challenge period enforced
5. Fraud proof → cheater penalized
6. Channel with insufficient funds → rejected
7. Expired challenge period → channel finalized

**`go test ./... -race` must pass.**
""",
        "security_task": """Security review BlockNet RPC, CLI, and state channels.

**RPC:**
1. Input validation — can malformed JSON-RPC crash server?
2. DoS — are there request size limits? Rate limiting?
3. Information disclosure — do error responses leak internals?
4. Authentication — should any endpoints require auth?

**State channels:**
1. CRITICAL: Can fraud proofs be forged?
2. Can challenge period be bypassed?
3. Are signed states replay-protected (include channel ID + nonce)?
4. Can channel funds be drained by manipulating close sequence?
5. What happens if both parties submit conflicting close at same time?
6. Integer overflow in channel balance accounting?

**CLI:**
1. Key storage — encrypted at rest? File permissions?
2. Command injection via arguments?
""",
        "review_task": """Code review BlockNet RPC, CLI, and state channels.

**Check:**
1. JSON-RPC spec compliance — proper error codes, batch support
2. WebSocket — connection lifecycle, cleanup on disconnect
3. CLI — cobra command structure, consistent flag naming
4. State channels — correct game theory (incentive-compatible)
5. Code reuse — tx signing shared between CLI and RPC
6. Error handling — user-facing errors are clear, not stack traces
7. Integration points — channels ready for DragonHoard/Loom integration
""",
    },
    {
        "id": 6,
        "name": "Performance Tuning — 2500 TPS Benchmark",
        "developer_task": """Performance-tune BlockNet to hit 2500 TPS at 500ms blocks.

**Benchmark harness:**
1. Transaction generator: create N signed txs (transfer type) with sequential nonces
2. Block builder: pack txs into blocks, measure time
3. State execution: apply block, measure state transition time
4. End-to-end: generate 2500 txs → build block → execute → commit → measure total

**Optimization targets:**
1. Transaction validation — batch signature verification (parallel)
2. State execution — minimize DB writes per block (batch commit)
3. Mempool — O(log n) insert, O(1) pop for block building
4. PoH — ensure verification doesn't bottleneck block processing
5. Serialization — benchmark protobuf vs msgpack for tx encoding
6. Memory — profile allocations per block, minimize GC pressure

**Deliverables:**
- `internal/bench/bench_test.go` — Go benchmark suite
- `cmd/habeas/cmd_bench.go` — `habeas bench` CLI command
- Optimization commits with before/after numbers
- pprof CPU + memory profiles saved for analysis

**Acceptance criteria:**
- `habeas bench --txs 2500 --block-time 500ms` → PASSES
- Block production under 400ms (100ms headroom)
- State commit under 100ms
- Memory per block < 50MB
- No GC pause > 5ms during benchmark
""",
        "tester_task": """Validate BlockNet performance benchmarks.

**Run and verify:**
1. `go test -bench=. -benchmem ./internal/bench/` — capture results
2. Verify 2500 TPS is achieved consistently (5 runs, all pass)
3. Profile memory: `go test -bench=. -memprofile mem.prof`
4. Profile CPU: `go test -bench=. -cpuprofile cpu.prof`
5. Check for regressions: compare with baseline if exists
6. Stress test: 10K TPS attempt — document where it breaks
7. Sustained load: run 2500 TPS for 100 blocks — no degradation
8. GC analysis: `GODEBUG=gctrace=1` during benchmark

**Output:** Performance report with numbers, profiles, and any bottlenecks found.
""",
        "security_task": """Security review BlockNet under load.

**Focus:**
1. Does batch signature verification maintain correctness?
2. Can performance optimizations introduce timing side channels?
3. Memory limits — can large blocks cause OOM?
4. Block size limits — enforced? What's the max tx per block?
5. Transaction ordering under load — fair? Or manipulable?
6. State commit atomicity — if commit fails mid-block, state consistent?
""",
        "review_task": """Code review BlockNet performance optimizations.

**Check:**
1. Benchmark methodology — is it realistic? Representative workload?
2. Optimization trade-offs — did we sacrifice correctness for speed?
3. Parallelism — goroutine pool properly bounded?
4. Memory management — sync.Pool usage appropriate?
5. Batch operations — error handling (what if one tx in batch fails?)
6. Profiling code — not included in production binary?
7. Benchmark reproducibility — deterministic seeds, fixed workload
""",
    },
]


# --- EQUIPA Python Rewrite Phases ---
# Portable installer rewrite with best practices.

EQUIPA_PHASES = [
    {
        "id": 1,
        "name": "Foundation — CLI Framework, Config, Package Detection",
        "developer_task": """Rewrite the EQUIPA portable installer foundation using Python best practices.

**Requirements:**
1. Modern Python CLI using `click` (not argparse)
2. Config via TOML (`equipa.toml`) with schema validation via pydantic
3. Package detection — scan system for: Python, Node.js, Docker, Git, Claude Code
4. Cross-platform path handling (Windows + Linux + macOS)
5. Rich terminal output via `rich` library (progress bars, tables, colored status)
6. Proper Python project structure with pyproject.toml
7. Type hints on all functions (strict mypy)

**Files:**
- `src/equipa/cli.py` — Click CLI entry point
- `src/equipa/config.py` — Pydantic config model + TOML loading
- `src/equipa/detect.py` — Package/tool detection
- `src/equipa/display.py` — Rich output helpers
- `pyproject.toml` — Project metadata, deps, scripts
- `tests/` — pytest test suite

**Acceptance criteria:**
- `uv run equipa --help` shows commands
- `uv run equipa detect` lists installed tools with versions
- Config loads from TOML with validation errors on bad input
- Works on Windows and Linux
""",
        "tester_task": """Write pytest tests for EQUIPA foundation.

**Test:** Config loading/validation, package detection (mocked), CLI help output, Rich formatting.
Use `pytest --strict-markers -v`. Aim for 90%+ coverage.
""",
        "security_task": """Security review EQUIPA foundation.

**Focus:** Config file handling (TOML injection?), subprocess calls for detection (command injection?),
path traversal in config paths, dependency audit.
""",
        "review_task": """Code review EQUIPA foundation.

**Check:** Python idioms, type hints, click best practices, pydantic model design, test quality.
""",
    },
]








BABEL_PHASES = [
    {
        "id": 1,
        "name": "Foundation -- Config, DB Pool, Schema, Health",
        "developer_task": """Build the Go API server foundation for Babel (The AI Language Forge).

**Requirements:**
1. Config loading via envconfig: DATABASE_URL, PORT, ANTHROPIC_API_KEY, JWT_SECRET, DH_API_URL, DH_API_KEY, FORGEBRIDGE_URL, ELEVENLABS_API_KEY
2. PostgreSQL connection pool (pgxpool), health check, graceful shutdown
3. Chi router with /api/health endpoint, CORS, request ID middleware
4. Full database schema from CLAUDE.md: language_families, languages, words, glyphs, literary_works, linguists, debates, phrases, user_profiles, commissions, debate_votes, generation_log
5. All CHECK constraints, JSONB columns, GIN indexes, foreign keys as specified in CLAUDE.md
6. sqlc setup with query files for all tables (basic CRUD)
7. River job queue initialization

Read CLAUDE.md for the complete schema. Implement EVERY table and constraint.
""",
        "tester_task": """Test: go build compiles, schema creates all tables, health endpoint returns 200, sqlc generates correctly, all CHECK constraints work.""",
        "security_task": """Review: SQL injection in queries, config handling (no secrets in code), connection pool limits, input validation.""",
        "review_task": """Review: Go idioms, error handling, project structure, sqlc query quality, schema completeness vs CLAUDE.md.""",
    },
    {
        "id": 2,
        "name": "Language Generation Pipeline",
        "developer_task": """Build the core language generation pipeline for Babel using River jobs and Claude API.

**Requirements:**
1. LangCoreGenJob: Generate complete phonology, morphology, and syntax from cultural parameters via Claude API
2. VocabGenJob: Generate vocabulary in batches of 50 words per semantic domain with IPA, etymology, example sentences
3. PhraseBookGenJob: Generate phrases across 15 categories
4. All jobs parse structured JSON from Claude responses into Go structs
5. Prompt templates that include full linguistic context
6. Retry logic: re-prompt with specific violations on validation failure (max 2 retries)

Use the Anthropic Go SDK. River for job queue. Each job is transactional with the DB insert.
""",
        "tester_task": """Test: River jobs register and can be enqueued, Claude prompt templates render, JSON parsing works, validation catches invalid phonotactics.""",
        "security_task": """Review: API key handling, prompt injection in user-supplied culture params, rate limiting on generation, cost tracking.""",
        "review_task": """Review: Job design, error handling, prompt engineering quality, struct design for linguistic data.""",
    },
    {
        "id": 3,
        "name": "Linguistic Consistency Engine + Validation",
        "developer_task": """Build the linguistic consistency engine that validates ALL generated content.

**Requirements:**
1. Phonotactic validator: syllabify IPA strings, check onset/coda clusters, validate vowel harmony
2. Morphological validator: verify derived words use correct affixes, check agreement rules
3. Etymology validator: ensure related words share root morphemes
4. Cultural validator: vocabulary richness matches cultural context
5. Context builder: load full linguistic state before any generation
6. Post-generation pipeline: validate then commit or re-prompt with violations

This is the CORE differentiator.
""",
        "tester_task": """Test: phonotactic validator catches invalid clusters, vowel harmony violations detected, etymology chains validated.""",
        "security_task": """Review: validator bypass, resource exhaustion in validation loops, data integrity.""",
        "review_task": """Review: algorithm correctness for syllabification, validator completeness, performance.""",
    },
    {
        "id": 4,
        "name": "Writing System + Audio + Literature",
        "developer_task": """Build writing system generation, audio pronunciation, and literature generation.

**Requirements:**
1. WritingGenJob: Design writing system via Claude, generate glyph images via ForgeBridge HTTP client
2. AudioGenJob: Generate pronunciation audio via ElevenLabs API, store audio URLs
3. LiteratureGenJob: Generate texts IN the language with interlinear glossing and free translation
4. ForgeBridge HTTP client for image generation
5. ElevenLabs HTTP client for TTS
6. All as River jobs with retry logic
""",
        "tester_task": """Test: glyph records with image URLs, audio URLs valid, literature has proper interlinear format.""",
        "security_task": """Review: external API key handling, SSRF in URLs, content validation.""",
        "review_task": """Review: HTTP client design, error handling for external APIs, job retry logic.""",
    },
    {
        "id": 5,
        "name": "REST API + Auth + DH Economy",
        "developer_task": """Build complete REST API with auth and DragonHoard economy.

**Requirements:**
1. JWT auth: issue/refresh/revoke, middleware, user profile on first login
2. REST endpoints: languages CRUD, words (paginated/searchable), glyphs, literature, phrases, debates with voting, commissions, linguists, full-text search
3. Rate limiting per endpoint per CLAUDE.md
4. DH economy: Go HTTP client, credit for voting/learning, debit for commissions
5. Idempotency keys for all DH transactions
6. Full-text search using PostgreSQL GIN indexes
7. Consistent JSON envelope: {data, meta, error}
""",
        "tester_task": """Test: all endpoints correct status codes, auth blocks unauthenticated, search works, DH idempotency.""",
        "security_task": """Review: JWT implementation, rate limiting, IDOR, SQL injection, DH transaction integrity.""",
        "review_task": """Review: API design consistency, error handling, middleware chain, search implementation.""",
    },
]


# Map project names to phase lists
PROJECT_PHASES = {
    "apocrypha": PHASES,
    "chain-node": BLOCK_NET_PHASES,
    "equipa": EQUIPA_PHASES,
    "babel": BABEL_PHASES,
}


def log(msg, output=None):
    """Log with timestamp."""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [Arena] {msg}"
    print(line, flush=True)
    if output:
        output.write(line + "\n")
        output.flush()


def get_db():
    """Get TheForge DB connection."""
    conn = sqlite3.connect(str(THEFORGE_DB))
    conn.row_factory = sqlite3.Row
    return conn


def create_task(conn, title, description, role, complexity="medium", task_type="feature"):
    """Create a task in TheForge and return its ID."""
    cursor = conn.execute(
        """INSERT INTO tasks (project_id, title, description, status, priority, role, complexity, task_type)
           VALUES (?, ?, ?, 'todo', 'high', ?, ?, ?)""",
        (PROJECT_ID, title, description, role, complexity, task_type),
    )
    conn.commit()
    return cursor.lastrowid


def get_task_status(conn, task_id):
    """Get task status and outcome."""
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def get_episodes_since(conn, start_time):
    """Get all episodes logged since a timestamp."""
    rows = conn.execute(
        """SELECT id, task_id, role, outcome, q_value, turns_used, approach_summary, reflection
           FROM agent_episodes
           WHERE created_at >= ? AND project_id = ?
           ORDER BY id""",
        (start_time, PROJECT_ID),
    ).fetchall()
    return [dict(r) for r in rows]


def dispatch_task(task_id, role=None):
    """Dispatch a single task via forge_orchestrator.

    Returns (success: bool, duration_seconds: float).
    """
    cmd = ["python3", "-u", str(ORCHESTRATOR)]

    # Use --dev-test for developer tasks (includes tester loop)
    # Use --task with --role for other roles
    if role and role != "developer":
        cmd += ["--task", str(task_id), "--role", role, "-y"]
    else:
        cmd += ["--task", str(task_id), "--dev-test", "-y"]

    log(f"Dispatching task {task_id} (role={role}): {' '.join(cmd)}")
    start = time.time()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=DISPATCH_TIMEOUT,
            cwd=str(SCRIPT_DIR),
        )
        duration = time.time() - start

        # Save output to log file
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / f"task_{task_id}_{role}_{int(time.time())}.log"
        with open(log_file, "w") as f:
            f.write(f"=== STDOUT ===\n{result.stdout}\n\n=== STDERR ===\n{result.stderr}")

        success = result.returncode == 0
        log(f"Task {task_id} finished: rc={result.returncode}, duration={duration:.0f}s, log={log_file.name}")

        return success, duration

    except subprocess.TimeoutExpired:
        duration = time.time() - start
        log(f"Task {task_id} TIMED OUT after {duration:.0f}s")
        return False, duration


def check_convergence(conn, phase_id, iteration):
    """Check if a phase has converged (all roles report clean).

    Looks at the latest episodes for this phase's tasks to determine
    if tester, security, and code-reviewer all passed cleanly.
    """
    # Simple heuristic: if the latest episode for each non-dev role has
    # a q_value >= 0.7, consider it converged
    rows = conn.execute(
        """SELECT role, outcome, q_value
           FROM agent_episodes
           WHERE project_id = ?
           ORDER BY id DESC
           LIMIT 10""",
        (PROJECT_ID,),
    ).fetchall()

    if not rows:
        return False

    role_results = {}
    for r in rows:
        role = r["role"]
        if role not in role_results:
            role_results[role] = {"outcome": r["outcome"], "q_value": r["q_value"]}

    # Check if we have results for all roles and they're positive
    clean_roles = 0
    for role in ["tester", "security-reviewer", "code-reviewer"]:
        result = role_results.get(role)
        if result and result["q_value"] >= 0.6:
            clean_roles += 1

    return clean_roles >= 2  # At least 2 of 3 non-dev roles report clean


def build_fix_description(phase, role_findings):
    """Build a developer fix task description from other roles' findings."""
    parts = [
        f"Fix issues found during {phase['name']} review.\n",
        "**Issues to address:**\n",
    ]

    for role, findings in role_findings.items():
        parts.append(f"\n### {role.title()} Findings:")
        parts.append(findings)

    parts.append(
        "\n\n**Requirements:**"
        "\n- Address ALL findings listed above"
        "\n- Do not introduce new issues while fixing"
        "\n- Run `go build ./...` and `go vet ./...` after fixes"
        "\n- Commit with descriptive message"
    )

    return "\n".join(parts)


def run_phase(phase, conn, dry_run=False, max_iterations=4, cooldown=10):
    """Run a full improvement cycle for one phase.

    Returns (iterations_used: int, converged: bool).
    """
    phase_id = phase["id"]
    phase_name = phase["name"]

    log(f"")
    log(f"{'='*70}")
    log(f"  PHASE {phase_id}: {phase_name}")
    log(f"{'='*70}")

    if dry_run:
        log(f"  [DRY RUN] Would create tasks for: developer, tester, security-reviewer, code-reviewer")
        log(f"  [DRY RUN] Max {max_iterations} iterations")
        return 0, False

    for iteration in range(1, max_iterations + 1):
        log(f"")
        log(f"--- Phase {phase_id}, Iteration {iteration}/{MAX_ITERATIONS_PER_PHASE} ---")

        # Step 1: Developer builds/fixes
        if iteration == 1:
            dev_desc = phase["developer_task"]
            dev_title = f"[Arena P{phase_id}] {phase_name} — Implementation"
        else:
            dev_desc = build_fix_description(phase, role_findings)
            dev_title = f"[Arena P{phase_id}] {phase_name} — Fix iteration {iteration}"

        dev_task_id = create_task(conn, dev_title, dev_desc, "developer", "complex")
        log(f"Created developer task #{dev_task_id}")
        dispatch_task(dev_task_id, role="developer")
        time.sleep(cooldown)

        # Step 2: Tester validates
        test_task_id = create_task(
            conn,
            f"[Arena P{phase_id}] {phase_name} — Testing (iter {iteration})",
            phase["tester_task"],
            "tester",
            "medium",
            "test",
        )
        log(f"Created tester task #{test_task_id}")
        dispatch_task(test_task_id, role="tester")
        time.sleep(cooldown)

        # Step 3: Security review
        sec_task_id = create_task(
            conn,
            f"[Arena P{phase_id}] {phase_name} — Security (iter {iteration})",
            phase["security_task"],
            "security-reviewer",
            "medium",
            "security_review",
        )
        log(f"Created security-reviewer task #{sec_task_id}")
        dispatch_task(sec_task_id, role="security-reviewer")
        time.sleep(cooldown)

        # Step 4: Code review
        review_task_id = create_task(
            conn,
            f"[Arena P{phase_id}] {phase_name} — Code Review (iter {iteration})",
            phase["review_task"],
            "code-reviewer",
            "medium",
            "code_review",
        )
        log(f"Created code-reviewer task #{review_task_id}")
        dispatch_task(review_task_id, role="code-reviewer")
        time.sleep(cooldown)

        # Step 5: Check for convergence
        if check_convergence(conn, phase_id, iteration):
            log(f"Phase {phase_id} CONVERGED after {iteration} iterations!")
            return iteration, True

        # Step 6: Collect findings for the fix task
        # Read the latest task outputs to build the fix description
        role_findings = {}
        for task_id, role_name in [
            (test_task_id, "tester"),
            (sec_task_id, "security-reviewer"),
            (review_task_id, "code-reviewer"),
        ]:
            log_files = sorted(LOG_DIR.glob(f"task_{task_id}_*"))
            if log_files:
                content = log_files[-1].read_text(errors="replace")
                # Extract the meaningful part (last 2000 chars of stdout)
                stdout_section = content.split("=== STDERR ===")[0] if "=== STDERR ===" in content else content
                role_findings[role_name] = stdout_section[-2000:]
            else:
                role_findings[role_name] = "(No output captured)"

        log(f"Phase {phase_id} iteration {iteration} complete. Findings collected for next iteration.")

    log(f"Phase {phase_id} reached max iterations ({max_iterations}) without full convergence.")
    return MAX_ITERATIONS_PER_PHASE, False


def export_lora_data(conn):
    """Export all Arena episodes as LoRA-ready ChatML JSONL.

    Converts agent_episodes into instruction/response pairs suitable
    for QLoRA fine-tuning with Unsloth.
    """
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    rows = conn.execute(
        """SELECT ae.*, t.title, t.description as task_description
           FROM agent_episodes ae
           LEFT JOIN tasks t ON ae.task_id = t.id
           WHERE ae.project_id = ?
           ORDER BY ae.id""",
        (PROJECT_ID,),
    ).fetchall()

    if not rows:
        log("No episodes to export.")
        return

    # Convert to ChatML format
    chatml_examples = []
    for row in rows:
        row = dict(row)

        # Build instruction from task description
        instruction = row.get("task_description", "") or ""
        if not instruction:
            instruction = f"Complete the following {row['role']} task: {row.get('title', 'Unknown')}"

        # Build response from approach + reflection
        response_parts = []
        if row.get("approach_summary"):
            response_parts.append(f"## Approach\n{row['approach_summary']}")
        if row.get("reflection"):
            response_parts.append(f"## Reflection\n{row['reflection']}")
        if row.get("outcome"):
            response_parts.append(f"## Outcome: {row['outcome']}")

        response = "\n\n".join(response_parts) if response_parts else "(no response captured)"

        # Only include episodes with reasonable content
        if len(instruction) < 20 or len(response) < 50:
            continue

        example = {
            "messages": [
                {"role": "system", "content": f"You are a {row['role']} agent. Complete the assigned task."},
                {"role": "user", "content": instruction},
                {"role": "assistant", "content": response},
            ],
            "metadata": {
                "role": row["role"],
                "outcome": row["outcome"],
                "q_value": row["q_value"],
                "turns_used": row["turns_used"],
                "task_id": row["task_id"],
                "source": "forge_arena",
            },
        }
        chatml_examples.append(example)

    # Write JSONL
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = EXPORT_DIR / f"arena_episodes_{timestamp}.jsonl"

    with open(output_file, "w") as f:
        for example in chatml_examples:
            f.write(json.dumps(example) + "\n")

    log(f"Exported {len(chatml_examples)} episodes to {output_file}")

    # Also write stats
    stats = {
        "total_episodes": len(rows),
        "exported_episodes": len(chatml_examples),
        "by_role": {},
        "by_outcome": {},
        "avg_q_value": 0,
    }
    for row in rows:
        row = dict(row)
        role = row["role"]
        outcome = row["outcome"]
        stats["by_role"][role] = stats["by_role"].get(role, 0) + 1
        stats["by_outcome"][outcome] = stats["by_outcome"].get(outcome, 0) + 1
    if rows:
        stats["avg_q_value"] = sum(dict(r)["q_value"] for r in rows) / len(rows)

    stats_file = EXPORT_DIR / f"arena_stats_{timestamp}.json"
    with open(stats_file, "w") as f:
        json.dump(stats, f, indent=2)

    log(f"Stats written to {stats_file}")
    return output_file


def main():
    parser = argparse.ArgumentParser(description="Forge Arena — Iterative Agent Training Loop")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without executing")
    parser.add_argument("--project", type=str, default=DEFAULT_PROJECT,
                        choices=list(PROJECT_PROFILES.keys()),
                        help=f"Project to train on (default: {DEFAULT_PROJECT})")
    parser.add_argument("--phase", type=int, help="Run specific phase only")
    parser.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS_PER_PHASE,
                        help=f"Max iterations per phase (default: {MAX_ITERATIONS_PER_PHASE})")
    parser.add_argument("--export-lora", action="store_true", help="Export episodes as LoRA training data")
    parser.add_argument("--resume-from", type=int, default=1, help="Resume from phase N (skip earlier phases)")
    parser.add_argument("--cooldown", type=int, default=COOLDOWN_BETWEEN_TASKS,
                        help=f"Seconds between task dispatches (default: {COOLDOWN_BETWEEN_TASKS})")
    args = parser.parse_args()

    # Set active project from --project flag
    global PROJECT_ID, PROJECT_CODENAME, PROJECT_DIR
    profile = PROJECT_PROFILES[args.project]
    PROJECT_ID = profile["id"]
    PROJECT_CODENAME = profile["codename"]
    PROJECT_DIR = profile["dir"]

    # Get the right phase list for this project
    active_phases = PROJECT_PHASES.get(args.project, PHASES)

    max_iter = args.max_iterations
    cooldown = args.cooldown

    conn = get_db()

    log(f"Forge Arena — Training Loop for {PROJECT_CODENAME} (project {PROJECT_ID})")
    log(f"Language: {profile['language']}, Dir: {PROJECT_DIR}")
    log(f"Phases: {len(active_phases)}, Max iterations/phase: {max_iter}")
    log(f"Roles per iteration: {' -> '.join(ROLE_ROTATION)}")
    log(f"")

    if args.export_lora:
        export_lora_data(conn)
        conn.close()
        return

    # Run phases
    phases_to_run = active_phases
    if args.phase:
        phases_to_run = [p for p in active_phases if p["id"] == args.phase]
        if not phases_to_run:
            print(f"ERROR: Phase {args.phase} not found (valid: 1-{len(active_phases)})")
            sys.exit(1)
    elif args.resume_from > 1:
        phases_to_run = [p for p in active_phases if p["id"] >= args.resume_from]

    results = []
    start_time = datetime.now().isoformat()

    for phase in phases_to_run:
        iterations, converged = run_phase(phase, conn, dry_run=args.dry_run,
                                          max_iterations=max_iter, cooldown=cooldown)
        results.append({
            "phase": phase["id"],
            "name": phase["name"],
            "iterations": iterations,
            "converged": converged,
        })

    # Summary
    log(f"")
    log(f"{'='*70}")
    log(f"  ARENA COMPLETE")
    log(f"{'='*70}")

    for r in results:
        status = "CONVERGED" if r["converged"] else f"MAX_ITER ({r['iterations']})"
        log(f"  Phase {r['phase']}: {r['name']} — {status}")

    # Count total episodes generated
    episodes = get_episodes_since(conn, start_time)
    log(f"")
    log(f"  Total episodes generated: {len(episodes)}")
    log(f"  Episodes by role: {{}}")

    # Auto-export if not dry run
    if not args.dry_run:
        log(f"")
        log(f"Exporting LoRA training data...")
        export_lora_data(conn)

    conn.close()


if __name__ == "__main__":
    main()

