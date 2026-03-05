# Effective Grep Patterns for Codebase Navigation

## Finding Entry Points

```bash
# API endpoints
grep -rn "app\.(get|post|put|delete|patch)" --include="*.{ts,js}"
grep -rn "@(Get|Post|Put|Delete|Patch|Route)" --include="*.py"
grep -rn "router\.(GET|POST|PUT|DELETE)" --include="*.go"
grep -rn "\[Http(Get|Post|Put|Delete)\]" --include="*.cs"

# Route definitions
grep -rn "path\(|url\(|urlpatterns" --include="*.py"
grep -rn "router\." --include="*.{ts,js}"
grep -rn "HandleFunc\|Handle\(" --include="*.go"
grep -rn "MapGet\|MapPost\|MapControllerRoute" --include="*.cs"
```

## Finding Where Something Lives

```bash
# By function name
grep -rn "def function_name\|func function_name\|function function_name"

# By class name
grep -rn "class ClassName"

# By error message (great for bug fixes)
grep -rn "exact error message text"

# By database table name
grep -rn "table_name\|TableName"

# By API endpoint path
grep -rn "/api/v[0-9]/endpoint"

# By environment variable
grep -rn "ENV_VAR_NAME\|process\.env\.\|os\.environ\|os\.Getenv"
```

## Finding Dependencies and Call Chains

```bash
# Who calls this function?
grep -rn "function_name(" --include="*.{ts,js,py,go}"

# Who imports this module?
grep -rn "from module import\|import module" --include="*.py"
grep -rn "require.*module\|import.*from.*module" --include="*.{ts,js}"

# Who uses this type/interface?
grep -rn "TypeName" --include="*.{ts,go,rs}"
```

## Finding Configuration

```bash
# Database connections
grep -rn "DATABASE_URL\|connection_string\|dsn\|DB_HOST"

# API keys and secrets
grep -rn "API_KEY\|SECRET\|TOKEN\|PASSWORD" --include="*.{env,yaml,yml,json,toml}"

# Feature flags
grep -rn "FEATURE_\|feature_flag\|isEnabled"
```

## Finding Test Infrastructure

```bash
# Test files
find . -name "*_test.*" -o -name "*.test.*" -o -name "test_*.*" | head -20

# Test configuration
find . -name "jest.config*" -o -name "pytest.ini" -o -name "phpunit.xml" -o -name ".mocharc*"

# Test commands in package managers
grep -n "test" package.json Makefile pyproject.toml 2>/dev/null
```

## Pro Tips

1. **Start broad, then narrow:** `grep -rn "keyword"` first, then add `--include` to filter
2. **Use `-l` for file list only:** When you just need to know WHICH files, not the matches
3. **Use `-c` for count:** When you need to know HOW MANY matches per file
4. **Exclude noise:** `--exclude-dir={node_modules,.git,vendor,__pycache__,dist,build}`
5. **Case insensitive for discovery:** `-i` flag when you're not sure of casing
