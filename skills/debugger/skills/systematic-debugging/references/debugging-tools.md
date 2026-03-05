# Debugging Tools Reference

Language-specific debugging commands and tools. Use these when you need to inspect
runtime state, trace execution, or profile performance.

## Python

### Quick Inspection
```bash
# Run with verbose traceback
python -v script.py

# Run a single test with full output
pytest -xvs test_file.py::test_name

# Check syntax without running
python -m py_compile script.py

# Find where a module is loaded from
python -c "import module; print(module.__file__)"
```

### Runtime Debugging
```python
# Insert breakpoint (Python 3.7+)
breakpoint()

# Print type and value at a decision point
print(f"DEBUG: {type(var)=}, {var=}")

# Trace function calls
import traceback
traceback.print_stack()

# Memory usage
import sys
sys.getsizeof(obj)

# Time a block
import time
start = time.perf_counter()
# ... code ...
print(f"Took {time.perf_counter() - start:.3f}s")
```

### Database Debugging (SQLite)
```python
# Enable SQLite tracing
import sqlite3
conn = sqlite3.connect('db.sqlite')
conn.set_trace_callback(print)

# Check database integrity
cursor.execute("PRAGMA integrity_check")
print(cursor.fetchone())

# List all tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")

# Check if column exists
cursor.execute("PRAGMA table_info(table_name)")
```

### Database Debugging (PostgreSQL)
```bash
# Check active connections
psql -c "SELECT pid, state, query FROM pg_stat_activity WHERE datname = 'dbname';"

# Check locks
psql -c "SELECT * FROM pg_locks WHERE NOT granted;"

# Analyze slow query
psql -c "EXPLAIN ANALYZE SELECT ..."
```

## JavaScript / TypeScript

### Quick Inspection
```bash
# Run with full stack traces
node --stack-trace-limit=100 script.js

# Run TypeScript directly
npx tsx script.ts

# Check for syntax errors
node --check script.js

# Run single test
npx jest --testPathPattern="test_name" --verbose
npx vitest run test_file.ts
```

### Runtime Debugging
```javascript
// Inspect object fully
console.dir(obj, { depth: null, colors: true });

// Time a block
console.time('label');
// ... code ...
console.timeEnd('label');

// Trace function calls
console.trace('label');

// Check event loop lag (Node.js)
const start = process.hrtime.bigint();
setImmediate(() => {
  const lag = Number(process.hrtime.bigint() - start) / 1e6;
  console.log(`Event loop lag: ${lag}ms`);
});

// Memory usage (Node.js)
console.log(process.memoryUsage());
```

### Network Debugging
```bash
# Inspect HTTP responses
curl -v http://localhost:3000/api/endpoint

# Check what's listening on a port
lsof -i :3000        # macOS/Linux
netstat -ano | grep 3000   # Windows
```

## Go

### Quick Inspection
```bash
# Run with race detector
go run -race main.go

# Run single test verbosely
go test -v -run TestName ./package/

# Show test coverage
go test -coverprofile=coverage.out ./...
go tool cover -html=coverage.out

# Check for common bugs
go vet ./...

# Lint
golangci-lint run
```

### Runtime Debugging
```go
// Print with type info
fmt.Printf("DEBUG: %T %+v\n", val, val)

// Stack trace
import "runtime/debug"
debug.PrintStack()

// Memory stats
import "runtime"
var m runtime.MemStats
runtime.ReadMemStats(&m)
fmt.Printf("Alloc: %d MB\n", m.Alloc/1024/1024)

// Goroutine count
fmt.Println("Goroutines:", runtime.NumGoroutine())

// Profile CPU
import "runtime/pprof"
f, _ := os.Create("cpu.prof")
pprof.StartCPUProfile(f)
defer pprof.StopCPUProfile()
```

### Build Debugging
```bash
# Show all build flags and environment
go env

# Show dependency graph
go mod graph

# Check why a dependency is included
go mod why module/path

# Tidy dependencies
go mod tidy
```

## Rust

### Quick Inspection
```bash
# Run with backtrace
RUST_BACKTRACE=1 cargo run

# Full backtrace
RUST_BACKTRACE=full cargo run

# Run single test with output
cargo test test_name -- --nocapture

# Check without building
cargo check

# Clippy (lint)
cargo clippy -- -W clippy::all
```

### Runtime Debugging
```rust
// Debug print
dbg!(&variable);

// Pretty print
println!("{:#?}", variable);

// Compile-time type name
fn type_of<T>(_: &T) -> &'static str {
    std::any::type_name::<T>()
}
println!("Type: {}", type_of(&var));
```

## C# / .NET

### Quick Inspection
```bash
# Run with detailed errors
dotnet run --verbosity detailed

# Run specific test
dotnet test --filter "FullyQualifiedName~TestName"

# Check for build errors
dotnet build --no-restore

# List installed packages
dotnet list package
```

### Runtime Debugging
```csharp
// Conditional debugging
System.Diagnostics.Debug.WriteLine($"Value: {variable}");

// Stopwatch
var sw = System.Diagnostics.Stopwatch.StartNew();
// ... code ...
sw.Stop();
Console.WriteLine($"Elapsed: {sw.ElapsedMilliseconds}ms");

// Stack trace
Console.WriteLine(Environment.StackTrace);
```

## Git Debugging

```bash
# Find which commit introduced a bug
git bisect start
git bisect bad                    # Current commit is broken
git bisect good <known_good_hash> # This commit was working
# Git will checkout commits for you to test
# After testing each: git bisect good / git bisect bad
git bisect reset                  # When done

# Show changes in a specific file over time
git log -p -- path/to/file

# Find who last changed a specific line
git blame path/to/file

# Show what changed between two points
git diff commit1..commit2 -- path/to/file

# Find commits that touch a specific function
git log -p -S "function_name" -- "*.py"
```

## System-Level Debugging

```bash
# Check disk space
df -h

# Check memory usage
free -h          # Linux
vm_stat          # macOS

# Check running processes
ps aux | grep <process>

# Check open files (Linux/macOS)
lsof -p <pid>

# Check network connections
ss -tlnp         # Linux
netstat -tlnp    # Linux
lsof -i -P       # macOS

# Check environment variables
env | grep <pattern>
printenv <VAR>

# Check DNS resolution
nslookup hostname
dig hostname
```
