# Language-Specific Agent Prompts — Task #1585 Summary

## What Was Done

### 1. Language Detection (detect_project_language() — line ~5752)
The function was already implemented and detects 7 languages via marker files:

| Language | Marker Files |
|----------|-------------|
| Python | pyproject.toml, setup.py, requirements.txt, Pipfile, *.py |
| TypeScript | tsconfig.json |
| JavaScript | package.json (without tsconfig.json) |
| Go | go.mod |
| Rust | Cargo.toml |
| C# | *.csproj, *.sln |
| Java | pom.xml, build.gradle, build.gradle.kts |

Returns a dict with languages (list), frameworks (list), and primary (string).

Framework detection covers: Django, FastAPI, Flask (via pyproject.toml), Next.js, React, Express, Vue, Angular (via package.json), .NET (via csproj/sln), Maven/Gradle (via pom.xml/build.gradle).

### 2. Language Prompt Files (prompts/languages/)

7 markdown files, each providing language-specific coding guidelines:

| File | Content Focus | Status |
|------|--------------|--------|
| python.md | PEP 8, type hints, mutable defaults, bare except, async, pytest | Pre-existing |
| typescript.md | strict mode, any abuse, async, React patterns, null safety | Pre-existing |
| go.md | error wrapping, goroutine leaks, context.Context, defer, table tests | Pre-existing |
| csharp.md | async/await, IDisposable, LINQ, nullable refs, DI | Pre-existing |
| rust.md | ownership/borrowing, Result/Error, unsafe, lifetimes, tokio | NEW |
| java.md | null safety, try-with-resources, concurrency, Spring DI | NEW |
| javascript.md | strict mode, JSDoc types, async, common bugs, Node.js | NEW |

### 3. Prompt Injection (build_system_prompt() — lines 2293-2316)
Already integrated. After task-type prompts, the function:
1. Calls detect_project_language(project_dir)
2. Iterates all detected languages
3. Loads corresponding prompts/languages/{lang}.md if it exists
4. Appends framework note if non-build frameworks detected (excludes dotnet/maven/gradle)
5. Deduplicates via injected_langs set

### 4. Tests (tests/test_language_detection.py)
NEW — 43 tests covering:
- Single language detection for all 7 languages (11 tests)
- Multi-language and full-stack project detection (2 tests)
- Framework detection: Django, FastAPI, Next.js, React, Vue, Angular, Express (6 tests)
- Edge cases: empty project, JavaScript excluded when TypeScript present (3 tests)
- Return value structure validation (3 tests)
- Language prompt file existence and content validation (3 tests + 15 parameterized)

## Test Results
All 331 tests pass (288 existing + 43 new) in 4.75s with 1 deprecation warning.

## Files Changed
- prompts/languages/rust.md — NEW
- prompts/languages/java.md — NEW
- prompts/languages/javascript.md — NEW
- tests/test_language_detection.py — NEW
- LANG-PROMPTS-1585.md — NEW (this file)
