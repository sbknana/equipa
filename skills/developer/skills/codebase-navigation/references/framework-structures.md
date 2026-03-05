# Common Framework Project Structures

Quick reference for navigating unfamiliar codebases by framework.

## Python — Django
```
project/
  manage.py                    # Entry point
  project/settings.py          # Config
  app_name/
    urls.py                    # Route definitions (START HERE for endpoints)
    views.py                   # Handlers (business logic entry)
    models.py                  # Database models
    serializers.py             # API serialization
    forms.py                   # Form validation
    admin.py                   # Admin panel config
    tests/                     # Tests
    migrations/                # DB migrations (skip these)
```

## Python — FastAPI
```
app/
  main.py                     # Entry point + app creation
  api/
    routes/ or endpoints/      # Route handlers (START HERE)
    dependencies.py            # Dependency injection
  models/                     # Pydantic models + SQLAlchemy
  services/ or core/           # Business logic
  db/                          # Database connection + queries
  config.py or settings.py     # Configuration
```

## Python — Flask
```
app/
  __init__.py                  # App factory (START HERE)
  routes/ or views/            # Route handlers
  models.py                    # Database models
  forms.py                     # WTForms
  templates/                   # Jinja2 templates
  static/                      # CSS/JS/images
```

## JavaScript/TypeScript — Next.js
```
app/ or pages/                 # Routes (file-based routing, START HERE)
  layout.tsx                   # Root layout
  page.tsx                     # Home page
  api/                         # API routes
components/                    # React components
lib/ or utils/                 # Shared logic
prisma/                        # Database schema
public/                        # Static assets
next.config.js                 # Framework config
```

## JavaScript/TypeScript — Express
```
src/
  index.ts or app.ts           # Entry point (START HERE)
  routes/                      # Route definitions
  controllers/                 # Request handlers
  middleware/                  # Express middleware
  models/                     # Database models
  services/                   # Business logic
  utils/                      # Helpers
```

## Go — Standard Layout
```
cmd/
  server/main.go               # Entry point (START HERE)
internal/                      # Private packages
  handlers/ or api/            # HTTP handlers
  service/                     # Business logic
  repository/ or store/        # Database access
  models/ or types/            # Data structures
pkg/                           # Public packages
migrations/                    # DB migrations
```

## Go — Gin/Echo/Fiber
```
main.go                        # Entry point (START HERE)
handlers/ or controllers/      # Route handlers
middleware/                    # HTTP middleware
models/                        # Data structures
routes/                        # Route registration
services/                      # Business logic
```

## Rust — Actix/Axum
```
src/
  main.rs                      # Entry point (START HERE)
  routes/ or handlers/         # HTTP handlers
  models/                      # Data structures
  db/                          # Database access
  config.rs                    # Configuration
  error.rs                     # Error types
  lib.rs                       # Library root (for libs)
```

## C# — ASP.NET
```
Program.cs                     # Entry point (START HERE)
Controllers/                   # API controllers
Models/                        # Data models
Services/                      # Business logic
Data/                          # EF Core DbContext + migrations
appsettings.json               # Configuration
```

## Quick Detection Rules

| File/Pattern | Framework |
|-------------|-----------|
| `manage.py` | Django |
| `next.config.*` | Next.js |
| `package.json` has "next" | Next.js |
| `package.json` has "express" | Express |
| `go.mod` | Go |
| `Cargo.toml` | Rust |
| `*.csproj` | C# / .NET |
| `pyproject.toml` with fastapi | FastAPI |
| `requirements.txt` with flask | Flask |
| `requirements.txt` with django | Django |
