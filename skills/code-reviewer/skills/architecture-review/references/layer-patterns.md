# Layer Patterns Reference

Quick-reference for identifying layer violations and correct dependency directions
across common architectures. Use during architecture reviews to verify separation of concerns.

## Standard Layer Architecture

```
┌──────────────────────────────┐
│      Presentation Layer      │  Controllers, Handlers, CLI, UI
│      (HTTP, gRPC, CLI)       │  Parses input, formats output
├──────────────────────────────┤
│       Application Layer      │  Services, Use Cases
│       (Business Logic)       │  Orchestrates business rules
├──────────────────────────────┤
│        Domain Layer          │  Models, Entities, Value Objects
│        (Core Types)          │  Pure data + validation rules
├──────────────────────────────┤
│      Infrastructure Layer    │  Repositories, DB, HTTP clients
│      (I/O, External)        │  Talks to external world
└──────────────────────────────┘

ARROWS POINT DOWN: Upper layers import lower layers. NEVER upward.
```

## Dependency Direction Rules

| From → To | Allowed? | Example |
|-----------|----------|---------|
| Controller → Service | YES | Handler calls business logic |
| Service → Repository | YES | Business logic queries data |
| Service → Model | YES | Business logic uses types |
| Repository → Model | YES | Data layer returns domain types |
| Controller → Model | YES | Handler uses types for validation |
| **Service → Controller** | **NO** | Business logic must not know about HTTP |
| **Model → Service** | **NO** | Types must not contain business logic |
| **Model → Repository** | **NO** | Types must not query the database |
| **Repository → Service** | **NO** | Data layer must not call business logic |
| **Utils → Business Logic** | **NO** | Utilities must be generic |

## Framework-Specific Layer Patterns

### Django
```
views.py         → Presentation (handle HTTP request/response)
serializers.py   → Presentation (input/output formatting)
services.py      → Application (business logic — create this if missing)
models.py        → Domain + Infrastructure (Django merges these)
managers.py      → Infrastructure (custom querysets)
forms.py         → Presentation (input validation)
urls.py          → Presentation (routing)
admin.py         → Presentation (admin UI config)
```

**Common Django violations:**
- Business logic in views.py (should be in services.py)
- Complex queries in views.py (should be in managers.py)
- HTTP response formatting in models.py

### FastAPI
```
routers/         → Presentation (endpoint definitions)
schemas/         → Presentation (Pydantic models for request/response)
services/        → Application (business logic)
models/          → Domain (SQLAlchemy/ORM models)
repositories/    → Infrastructure (database queries)
dependencies/    → Cross-cutting (auth, DB sessions)
```

**Common FastAPI violations:**
- Database queries directly in router functions
- Business logic in dependency injection functions
- Pydantic schemas used as database models

### Express / NestJS
```
controllers/     → Presentation (route handlers)
services/        → Application (business logic)
models/          → Domain (Mongoose/Sequelize models)
repositories/    → Infrastructure (database queries)
middleware/      → Cross-cutting (auth, logging, CORS)
dto/             → Presentation (Data Transfer Objects)
```

**Common Express violations:**
- `res.json()` deep inside business logic
- Database queries in middleware
- Validation mixed with business logic

### Go (Clean Architecture)
```
cmd/             → Entry point (main.go, CLI)
internal/
  handler/       → Presentation (HTTP handlers)
  service/       → Application (business logic)
  repository/    → Infrastructure (database)
  model/         → Domain (types, interfaces)
pkg/             → Shared utilities (generic, no business logic)
```

**Common Go violations:**
- `http.ResponseWriter` passed into service functions
- `sql.DB` used directly in handlers
- Business types defined in handler package

### C# / ASP.NET
```
Controllers/     → Presentation (API endpoints)
Services/        → Application (business logic)
Models/          → Domain (entity classes)
Data/            → Infrastructure (DbContext, repositories)
DTOs/            → Presentation (request/response shapes)
Middleware/      → Cross-cutting (auth, error handling)
```

**Common .NET violations:**
- DbContext injected directly into controllers
- LINQ queries in controller actions
- ActionResult return types in service classes

## Anti-Pattern Detection

### Circular Dependencies
```
A imports B, B imports C, C imports A  ← CIRCULAR

Detection:
- Python: ImportError at startup
- Go: "import cycle not allowed" (compile error)
- JS/TS: undefined at require time (silent failure!)
- Rust: Compiler prevents module cycles

Fix: Extract shared types into a common module that both import.
```

### God Module Detection
```
Symptoms:
- File > 500 lines
- Module imported by > 10 other modules
- Contains functions from different domains (auth + billing + email)
- Filename is "utils.py", "helpers.ts", or "common.go"

Fix: Split by domain. "utils" should contain ONLY generic utilities
(string manipulation, date formatting, etc.), never business logic.
```

### Leaky Abstraction Detection
```
Symptoms:
- Function accepts `sql.DB` / `DbContext` but also HTTP request objects
- Return type includes framework-specific types (HttpResponse from service layer)
- Error messages reference infrastructure ("table not found" from business logic)
- Test requires database/HTTP to run what should be pure logic

Fix: Define interfaces at the boundary. Business logic accepts interfaces,
infrastructure implements them.
```

## Quick Checklist for Reviews

```
For each changed file:
1. What layer is this file in?
2. What does it import? (check for upward dependencies)
3. What imports it? (check for unexpected dependents)
4. Does it have responsibilities from multiple layers?
5. Could you test this file without infrastructure (DB, HTTP, filesystem)?
```
