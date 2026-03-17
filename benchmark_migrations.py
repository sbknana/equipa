#!/usr/bin/env python3
"""EQUIPA Migration Benchmark — Reproducible Demo

Creates temporary databases at each schema version (v1, v2, v3) with realistic
sample data, then runs every migration path and verifies zero data loss, backup
integrity, and schema correctness.

Usage:
    python benchmark_migrations.py

Requires: db_migrate.py in the same directory.
Stdlib only — no pip dependencies.

Copyright 2026 Forgeborn
"""

import hashlib
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# Ensure we can import db_migrate from the same directory
sys.path.insert(0, str(Path(__file__).parent))

from db_migrate import CURRENT_VERSION, run_migrations


# ============================================================
# V1 Schema: 19 core project-management tables
# ============================================================

V1_TABLES_DDL = """
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    codename TEXT,
    category TEXT,
    status TEXT DEFAULT 'active',
    summary TEXT,
    target_market TEXT,
    revenue_model TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    icon_base64 TEXT
);

CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'todo',
    priority TEXT DEFAULT 'medium',
    blocked_by TEXT,
    due_date DATE,
    completed_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    topic TEXT NOT NULL,
    decision TEXT NOT NULL,
    rationale TEXT,
    alternatives_considered TEXT,
    decided_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE open_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    context TEXT,
    priority TEXT DEFAULT 'medium',
    resolved INTEGER DEFAULT 0,
    resolution TEXT,
    asked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    resolved_at DATETIME,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE session_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    session_date DATE DEFAULT CURRENT_DATE,
    summary TEXT NOT NULL,
    key_points TEXT,
    next_steps TEXT,
    chat_url TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE code_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    language TEXT,
    code TEXT NOT NULL,
    purpose TEXT,
    file_path TEXT,
    version TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    type TEXT,
    specs TEXT,
    price REAL,
    currency TEXT DEFAULT 'USD',
    vendor TEXT,
    url TEXT,
    status TEXT DEFAULT 'candidate',
    notes TEXT,
    added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE competitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    product_name TEXT,
    price_range TEXT,
    strengths TEXT,
    weaknesses TEXT,
    url TEXT,
    notes TEXT,
    analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE content_tickler (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    total_posts INTEGER DEFAULT 0,
    posts_used INTEGER DEFAULT 0,
    posts_remaining INTEGER DEFAULT 0,
    alert_threshold INTEGER DEFAULT 4,
    last_checked DATE,
    needs_content INTEGER DEFAULT 0,
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE cross_references (
    id INTEGER PRIMARY KEY,
    source_table TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    target_table TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    relationship TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    type TEXT,
    file_path TEXT,
    description TEXT,
    version TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE posting_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    week_number INTEGER NOT NULL,
    day_of_week TEXT NOT NULL,
    platform TEXT NOT NULL,
    product TEXT NOT NULL,
    post_id TEXT NOT NULL,
    scheduled_date DATE,
    status TEXT DEFAULT 'pending',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE product_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    industry TEXT NOT NULL,
    opportunity_name TEXT,
    target_market TEXT NOT NULL,
    pain_points TEXT NOT NULL,
    existing_solutions TEXT,
    pricing_landscape TEXT,
    opportunity_score INTEGER DEFAULT 0,
    notes TEXT,
    status TEXT DEFAULT 'researched',
    researched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE project_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    asset_type TEXT NOT NULL,
    asset_name TEXT NOT NULL,
    description TEXT,
    is_primary BOOLEAN DEFAULT 0,
    file_path TEXT,
    base64_data TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE build_info (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    build_type TEXT NOT NULL,
    build_command TEXT NOT NULL,
    output_path TEXT,
    output_filename TEXT,
    prerequisites TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE reminders (
    id INTEGER PRIMARY KEY,
    project_id INTEGER,
    title TEXT NOT NULL,
    description TEXT,
    reminder_date DATE NOT NULL,
    command TEXT,
    status TEXT DEFAULT 'pending',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE research (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    topic TEXT NOT NULL,
    findings TEXT NOT NULL,
    source_url TEXT,
    source_name TEXT,
    relevance TEXT,
    researched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE social_media_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    post_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    product TEXT NOT NULL,
    content TEXT NOT NULL,
    hashtags TEXT,
    image_notes TEXT,
    status TEXT DEFAULT 'pending',
    posted_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE writing_style (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    style_element TEXT NOT NULL,
    description TEXT NOT NULL,
    examples TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE INDEX idx_tasks_project_status ON tasks(project_id, status);
CREATE INDEX idx_decisions_project ON decisions(project_id);
CREATE INDEX idx_open_questions_resolved ON open_questions(resolved);
CREATE INDEX idx_projects_status ON projects(status);
CREATE INDEX idx_components_project ON components(project_id);
CREATE INDEX idx_xref_source ON cross_references(source_table, source_id);
CREATE INDEX idx_xref_target ON cross_references(target_table, target_id);

CREATE TRIGGER update_project_timestamp
AFTER UPDATE ON projects
BEGIN
    UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

PRAGMA user_version = 1;
"""

# The 19 tables present in a v1 database
V1_TABLE_NAMES = [
    "projects", "tasks", "decisions", "open_questions", "session_notes",
    "code_artifacts", "components", "competitors", "content_tickler",
    "cross_references", "documents", "posting_schedule",
    "product_opportunities", "project_assets", "build_info", "reminders",
    "research", "social_media_posts", "writing_style",
]

# 9 tables added in v2 (total: 28)
V2_ADDED_TABLE_NAMES = [
    "agent_runs", "voice_messages", "api_keys", "lessons_learned",
    "agent_episodes", "forgesmith_runs", "forgesmith_changes",
    "rubric_scores", "rubric_evolution_history",
]

# 2 tables added in v3 (total: 30)
V3_ADDED_TABLE_NAMES = [
    "agent_messages", "agent_actions",
]


# ============================================================
# Realistic sample data generators
# ============================================================

def _insert_v1_data(conn):
    """Populate a v1 database with realistic sample data.

    Creates: 5 projects, 20 tasks, 10 decisions, 8 open questions,
    5 session notes, 3 code artifacts, 4 components, 3 competitors,
    2 content ticklers, 3 cross-refs, 2 documents, 4 posting schedules,
    2 product opportunities, 2 project assets, 2 build infos,
    3 reminders, 3 research entries, 4 social media posts, 3 writing styles.
    """
    # -- Projects (5) -------------------------------------------------
    projects = [
        ("TheForge", "TheForge", "infrastructure", "active",
         "MCP SQLite server for persistent AI context", "AI developers", "open-source"),
        ("TCGKungfu", "TCGKungfu", "saas", "active",
         "Trading card game inventory and POS kiosk", "Card shop owners", "subscription"),
        ("Loom", "Loom", "game", "active",
         "Narrative strategy game with AI-driven stories", "Gamers 18-35", "premium"),
        ("Apocrypha", "Apocrypha", "game", "planning",
         "Dark fantasy deckbuilder with procedural lore", "Strategy gamers", "premium"),
        ("DOGE-HABEAS", "DOGE-HABEAS", "civic-tech", "active",
         "Government spending transparency blockchain", "Citizens", "open-source"),
    ]
    conn.executemany(
        "INSERT INTO projects (name, codename, category, status, summary, target_market, revenue_model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", projects,
    )

    # -- Tasks (20) ---------------------------------------------------
    task_data = [
        (1, "Set up MCP server", "Configure SQLite MCP for Claude", "done", "high"),
        (1, "Build migration system", "Schema versioning and upgrades", "done", "high"),
        (1, "Add ForgeSmith tables", "Self-improvement tracking", "done", "medium"),
        (1, "Write benchmark script", "Demo migration performance", "in_progress", "medium"),
        (2, "Design kiosk UI", "Touch-friendly POS interface", "done", "high"),
        (2, "Implement tRPC routes", "Type-safe API layer", "done", "high"),
        (2, "Add barcode scanning", "USB barcode reader integration", "in_progress", "medium"),
        (2, "Pricing engine", "Dynamic pricing with market data", "todo", "high"),
        (2, "Multi-store sync", "Real-time inventory sync", "todo", "medium"),
        (3, "World generation", "Procedural world building system", "done", "high"),
        (3, "Google OAuth", "Social login for player accounts", "done", "high"),
        (3, "Narrative engine", "AI-driven story generation", "in_progress", "high"),
        (3, "Combat system", "Turn-based tactical combat", "todo", "high"),
        (3, "Multiplayer lobby", "Real-time matchmaking", "todo", "medium"),
        (4, "Lore generator", "Procedural dark fantasy lore", "todo", "high"),
        (4, "Card art pipeline", "AI-generated card illustrations", "todo", "medium"),
        (5, "Smart contract audit", "Third-party security audit", "blocked", "high"),
        (5, "Frontend dashboard", "Spending visualization portal", "in_progress", "medium"),
        (5, "API rate limiting", "Throttle public API endpoints", "todo", "medium"),
        (5, "Documentation site", "Public docs with Docusaurus", "todo", "low"),
    ]
    conn.executemany(
        "INSERT INTO tasks (project_id, title, description, status, priority) "
        "VALUES (?, ?, ?, ?, ?)", task_data,
    )

    # -- Decisions (10) -----------------------------------------------
    decisions = [
        (1, "Database", "Use SQLite over PostgreSQL",
         "Single-file portability, no server overhead", "PostgreSQL, DuckDB"),
        (1, "Migration strategy", "Use PRAGMA user_version for versioning",
         "Built into SQLite, no extra tables needed", "Alembic, custom table"),
        (2, "Framework", "Next.js 14 with App Router",
         "Server components, good DX, Vercel hosting", "Remix, SvelteKit"),
        (2, "ORM", "Prisma over Drizzle",
         "Better type generation, mature ecosystem", "Drizzle, TypeORM"),
        (3, "Game engine", "Custom web engine over Unity",
         "Web-first, no install friction", "Unity WebGL, Godot"),
        (3, "Auth provider", "Google OAuth via next-auth",
         "Largest user base, simple setup", "Discord, custom JWT"),
        (4, "Art style", "AI-generated with Midjourney pipeline",
         "Consistent dark fantasy aesthetic", "Commissioned artists"),
        (5, "Blockchain", "Ethereum L2 (Arbitrum)",
         "Low gas fees, EVM compatible", "Solana, Polygon, Base"),
        (5, "Frontend", "React with Vite",
         "Fast builds, good ecosystem", "Next.js, Vue"),
        (1, "Backup strategy", "Restic with 3-2-1 rule",
         "Dedup, encryption, multi-backend", "rsync, Borg"),
    ]
    conn.executemany(
        "INSERT INTO decisions (project_id, topic, decision, rationale, alternatives_considered) "
        "VALUES (?, ?, ?, ?, ?)", decisions,
    )

    # -- Open Questions (8) -------------------------------------------
    questions = [
        (1, "Should we support PostgreSQL as an alternative backend?",
         "Some users want server-based deployments", "low"),
        (2, "How to handle offline kiosk mode?",
         "Shops may lose internet during tournaments", "high"),
        (3, "What AI model for narrative generation?",
         "Need good creative writing at low cost", "high"),
        (3, "How many concurrent players per world?",
         "Affects server architecture decisions", "medium"),
        (4, "Release on Steam or itch.io first?",
         "Steam has more reach but higher cut", "medium"),
        (5, "Which L2 for lowest gas fees?",
         "Arbitrum vs Base vs Polygon", "high"),
        (2, "Accept cryptocurrency payments?",
         "Some card shops want crypto for high-value cards", "low"),
        (1, "Add real-time sync via WebSocket?",
         "Multi-device editing scenarios", "low"),
    ]
    conn.executemany(
        "INSERT INTO open_questions (project_id, question, context, priority) "
        "VALUES (?, ?, ?, ?)", questions,
    )

    # -- Session Notes (5) --------------------------------------------
    notes = [
        (1, "Built migration system with PRAGMA user_version detection",
         "Schema versioning, fingerprinting, backup before migrate",
         "Write benchmark script, test edge cases"),
        (2, "Implemented barcode scanning prototype",
         "USB HID reader works, need error handling for damaged barcodes",
         "Add retry logic, test with damaged cards"),
        (3, "Completed world generation for 6 seed worlds",
         "Procedural terrain, biomes, NPC placement all working",
         "Start narrative engine, connect world state to story"),
        (4, "Designed initial card template system",
         "Rarity tiers, element types, art frame layouts",
         "Build card renderer, test with sample art"),
        (5, "Deployed smart contracts to Arbitrum testnet",
         "Gas costs ~$0.003 per tx, well within budget",
         "Schedule security audit, build frontend dashboard"),
    ]
    conn.executemany(
        "INSERT INTO session_notes (project_id, summary, key_points, next_steps) "
        "VALUES (?, ?, ?, ?)", notes,
    )

    # -- Code Artifacts (3) -------------------------------------------
    artifacts = [
        (1, "db_migrate.py", "python",
         "def run_migrations(db_path): ...",
         "Database migration runner", "db_migrate.py", "1.0"),
        (2, "pricing-engine.ts", "typescript",
         "export function calculatePrice(card: Card): number { ... }",
         "Dynamic card pricing", "src/lib/pricing-engine.ts", "0.1"),
        (3, "world-gen.ts", "typescript",
         "export async function generateWorld(seed: number): Promise<World> { ... }",
         "Procedural world generation", "src/engine/world-gen.ts", "0.3"),
    ]
    conn.executemany(
        "INSERT INTO code_artifacts (project_id, name, language, code, purpose, file_path, version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", artifacts,
    )

    # -- Components (4) -----------------------------------------------
    components = [
        (2, "Barcode Scanner", "hardware", "USB HID, 1D/2D",
         49.99, "USD", "Amazon", "https://amazon.com/dp/B123", "purchased", "Works great"),
        (2, "Thermal Printer", "hardware", "80mm USB receipt",
         89.99, "USD", "Amazon", "https://amazon.com/dp/B456", "candidate", "Need to test"),
        (2, "Kiosk Display", "hardware", "15.6in touch IPS",
         299.99, "USD", "ASUS", "https://asus.com/displays/vt168", "purchased", "Mounted"),
        (5, "YubiKey 5", "security", "FIDO2/U2F hardware key",
         55.00, "USD", "Yubico", "https://yubico.com/yk5", "purchased", "For deploy signing"),
    ]
    conn.executemany(
        "INSERT INTO components (project_id, name, type, specs, price, currency, vendor, url, status, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", components,
    )

    # -- Competitors (3) ----------------------------------------------
    competitors = [
        (2, "TCGPlayer", "TCGPlayer Direct", "$10-50/mo",
         "Huge marketplace, price data", "Slow for in-store", "https://tcgplayer.com",
         "Main competitor for pricing data"),
        (2, "Crystal Commerce", "POS System", "$50-150/mo",
         "Industry standard for card shops", "Outdated UI, expensive", "https://crystalcommerce.com",
         "Legacy incumbent"),
        (3, "AI Dungeon", "AI Dungeon", "Free-$10/mo",
         "First-mover in AI narratives", "Quality inconsistent", "https://aidungeon.com",
         "Different genre but same AI narrative space"),
    ]
    conn.executemany(
        "INSERT INTO competitors (project_id, name, product_name, price_range, "
        "strengths, weaknesses, url, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        competitors,
    )

    # -- Content Tickler (2) ------------------------------------------
    content_ticklers = [
        (2, "twitter", 30, 18, 12, 4, "2026-03-01", 0, "Weekly card spotlights"),
        (2, "instagram", 20, 15, 5, 4, "2026-03-01", 1, "Need more product photos"),
    ]
    conn.executemany(
        "INSERT INTO content_tickler (project_id, platform, total_posts, posts_used, "
        "posts_remaining, alert_threshold, last_checked, needs_content, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", content_ticklers,
    )

    # -- Cross References (3) -----------------------------------------
    xrefs = [
        (1, "tasks", 1, "decisions", 1, "implements"),
        (2, "tasks", 5, "components", 3, "requires"),
        (3, "tasks", 10, "decisions", 5, "implements"),
    ]
    conn.executemany(
        "INSERT INTO cross_references (id, source_table, source_id, target_table, target_id, relationship) "
        "VALUES (?, ?, ?, ?, ?, ?)", xrefs,
    )

    # -- Documents (2) ------------------------------------------------
    documents = [
        (1, "Architecture Overview", "markdown", "docs/architecture.md",
         "High-level system design", "1.0"),
        (2, "API Reference", "openapi", "docs/api.yaml",
         "tRPC API documentation", "0.2"),
    ]
    conn.executemany(
        "INSERT INTO documents (project_id, name, type, file_path, description, version) "
        "VALUES (?, ?, ?, ?, ?, ?)", documents,
    )

    # -- Posting Schedule (4) -----------------------------------------
    schedule = [
        (2, 1, "Monday", "twitter", "TCGKungfu", "tcg-w1-mon", "2026-03-03", "posted"),
        (2, 1, "Wednesday", "instagram", "TCGKungfu", "tcg-w1-wed", "2026-03-05", "pending"),
        (2, 1, "Friday", "twitter", "TCGKungfu", "tcg-w1-fri", "2026-03-07", "pending"),
        (2, 2, "Monday", "twitter", "TCGKungfu", "tcg-w2-mon", "2026-03-10", "pending"),
    ]
    conn.executemany(
        "INSERT INTO posting_schedule (project_id, week_number, day_of_week, platform, "
        "product, post_id, scheduled_date, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        schedule,
    )

    # -- Product Opportunities (2) ------------------------------------
    opportunities = [
        ("Trading Cards", "Card Shop POS", "Local card shops",
         "No modern POS for TCG inventory", "Crystal Commerce, Square",
         "$50-150/mo typical", 85, "Strong niche demand", "validated"),
        ("Gaming", "AI Narrative Engine", "Story gamers",
         "Static game narratives, no replayability", "AI Dungeon, NovelAI",
         "$5-15/mo or premium", 70, "Growing market", "researched"),
    ]
    conn.executemany(
        "INSERT INTO product_opportunities (industry, opportunity_name, target_market, "
        "pain_points, existing_solutions, pricing_landscape, opportunity_score, notes, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", opportunities,
    )

    # -- Project Assets (2) -------------------------------------------
    assets = [
        (2, "logo", "TCGKungfu Logo", "Primary brand logo", 1, "assets/logo.svg", None),
        (3, "logo", "Loom Logo", "Game logo for storefront", 1, "assets/loom-logo.png", None),
    ]
    conn.executemany(
        "INSERT INTO project_assets (project_id, asset_type, asset_name, description, "
        "is_primary, file_path, base64_data) VALUES (?, ?, ?, ?, ?, ?, ?)", assets,
    )

    # -- Build Info (2) -----------------------------------------------
    builds = [
        (2, "production", "npm run build", ".next", "standalone",
         "Node.js 20+", "Uses standalone output"),
        (5, "contract", "forge build", "out", "SpendingTracker.sol",
         "Foundry", "Optimized for Arbitrum"),
    ]
    conn.executemany(
        "INSERT INTO build_info (project_id, build_type, build_command, output_path, "
        "output_filename, prerequisites, notes) VALUES (?, ?, ?, ?, ?, ?, ?)", builds,
    )

    # -- Reminders (3) ------------------------------------------------
    reminders = [
        (1, 2, "Renew domain", "forgeborn.dev renewal due", "2026-04-15",
         None, "pending"),
        (2, 5, "Security audit deadline", "Smart contract audit due", "2026-03-20",
         None, "pending"),
        (3, None, "Check GPU prices", "RTX 5090 launch window", "2026-03-10",
         None, "pending"),
    ]
    conn.executemany(
        "INSERT INTO reminders (id, project_id, title, description, reminder_date, "
        "command, status) VALUES (?, ?, ?, ?, ?, ?, ?)", reminders,
    )

    # -- Research (3) -------------------------------------------------
    research_data = [
        (2, "TCG Market Size", "US TCG market worth $12.2B in 2025",
         "https://icv2.com/articles/market/view/12345", "ICV2", "high"),
        (3, "AI Game Narrative SOTA", "GPT-4o and Claude generate coherent multi-chapter stories",
         "https://arxiv.org/abs/2025.12345", "ArXiv", "high"),
        (5, "L2 Gas Comparison", "Arbitrum: $0.003, Base: $0.001, Polygon: $0.002 per tx",
         "https://l2fees.info", "L2Fees", "high"),
    ]
    conn.executemany(
        "INSERT INTO research (project_id, topic, findings, source_url, source_name, relevance) "
        "VALUES (?, ?, ?, ?, ?, ?)", research_data,
    )

    # -- Social Media Posts (4) ---------------------------------------
    posts = [
        (2, "tcg-post-001", "twitter", "TCGKungfu",
         "Introducing TCGKungfu - the modern POS built for card shops!",
         "#tcg #pokemon #mtg", "Product screenshot", "posted"),
        (2, "tcg-post-002", "twitter", "TCGKungfu",
         "Barcode scanning makes inventory a breeze",
         "#tcg #inventory", "Demo video", "posted"),
        (2, "tcg-post-003", "instagram", "TCGKungfu",
         "Beautiful kiosk display showing card prices in real-time",
         "#cardshop #pos #tcg", "Kiosk photo", "pending"),
        (3, "loom-post-001", "twitter", "Loom",
         "Every playthrough tells a different story. Loom - coming soon.",
         "#indiegame #ai #narrative", "Concept art", "pending"),
    ]
    conn.executemany(
        "INSERT INTO social_media_posts (project_id, post_id, platform, product, "
        "content, hashtags, image_notes, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", posts,
    )

    # -- Writing Style (3) --------------------------------------------
    styles = [
        (2, "tone", "Casual and enthusiastic",
         "Check out our latest feature! / This is gonna change everything."),
        (2, "voice", "Direct, second-person",
         "You scan it. We price it. Done."),
        (3, "tone", "Mysterious and evocative",
         "The threads of fate are yours to weave. / Every choice echoes."),
    ]
    conn.executemany(
        "INSERT INTO writing_style (project_id, style_element, description, examples) "
        "VALUES (?, ?, ?, ?)", styles,
    )

    conn.commit()


def _insert_v2_data(conn):
    """Add v2-specific sample data on top of v1 data.

    Creates: 50 agent runs, 10 lessons learned, 20 agent episodes,
    5 forgesmith runs, 8 forgesmith changes, 15 rubric scores,
    6 rubric evolution history entries, 3 voice messages, 2 API keys.
    """
    # -- Agent Runs (50) ----------------------------------------------
    roles = ["developer", "tester", "planner", "evaluator", "security-reviewer",
             "code-reviewer", "debugger", "frontend-designer", "integration-tester"]
    models = ["claude-opus-4-20250514", "claude-sonnet-4-20250514"]
    outcomes = ["success", "partial", "failure", "timeout", "early_termination"]

    agent_runs = []
    for i in range(50):
        role = roles[i % len(roles)]
        model = models[i % len(models)]
        project_id = (i % 5) + 1
        task_id = (i % 20) + 1
        turns = 5 + (i * 3) % 40
        duration = 30.0 + (i * 7.3) % 300
        cost = round(0.01 + (i * 0.037) % 0.5, 4)
        success = 1 if i % 3 != 2 else 0
        outcome = outcomes[i % len(outcomes)]
        agent_runs.append((task_id, project_id, role, model, turns, duration,
                           cost, success, outcome, f"Output tail for run {i+1}..."))
    conn.executemany(
        "INSERT INTO agent_runs (task_id, project_id, role, model, turns_used, "
        "duration_s, cost_usd, success, outcome, output_tail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", agent_runs,
    )

    # -- Lessons Learned (10) -----------------------------------------
    lessons = [
        (1, "developer", "file_not_found", "Missing import path",
         "Always verify file exists before importing", "forgesmith"),
        (1, "tester", "assertion_error", "Wrong expected value",
         "Use dynamic assertions based on actual schema", "forgesmith"),
        (2, "developer", "timeout", "Infinite loop in retry",
         "Add max_retries parameter to all retry loops", "forgesmith"),
        (2, "code-reviewer", "style_violation", "Inconsistent naming",
         "Follow project naming conventions in CLAUDE.md", "forgesmith"),
        (3, "developer", "syntax_error", "Missing comma in JSON",
         "Validate JSON before writing to file", "manual"),
        (3, "tester", "flaky_test", "Race condition in async test",
         "Use proper async/await patterns in test setup", "forgesmith"),
        (None, "developer", "permission_denied", "Can't write to /etc",
         "Check file permissions before write operations", "forgesmith"),
        (None, "debugger", "stack_overflow", "Recursive call without base case",
         "Always add base case check for recursive functions", "forgesmith"),
        (4, "planner", "scope_creep", "Task too broadly defined",
         "Break tasks into specific, measurable deliverables", "manual"),
        (5, "security-reviewer", "sql_injection", "Unparameterized query",
         "Always use parameterized queries, never string concat", "forgesmith"),
    ]
    conn.executemany(
        "INSERT INTO lessons_learned (project_id, role, error_type, error_signature, "
        "lesson, source) VALUES (?, ?, ?, ?, ?, ?)", lessons,
    )

    # -- Agent Episodes (20) ------------------------------------------
    episodes = []
    for i in range(20):
        role = roles[i % len(roles)]
        task_type = ["code_write", "code_review", "test_write", "debug", "plan"][i % 5]
        project_id = (i % 5) + 1
        task_id = (i % 20) + 1
        turns = 8 + (i * 5) % 30
        outcome = outcomes[i % len(outcomes)]
        q_val = round(0.3 + (i * 0.035), 3)
        episodes.append((task_id, role, task_type, project_id,
                         f"Approach: {task_type} using standard patterns",
                         turns, outcome, None, f"Reflection on episode {i+1}",
                         min(q_val, 0.95)))
    conn.executemany(
        "INSERT INTO agent_episodes (task_id, role, task_type, project_id, "
        "approach_summary, turns_used, outcome, error_patterns, reflection, q_value) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", episodes,
    )

    # -- ForgeSmith Runs (5) ------------------------------------------
    fs_runs = [
        ("fs-run-001", 10, 3, "Analyzed 10 runs, made 3 prompt adjustments", "auto"),
        ("fs-run-002", 15, 5, "Weight rebalancing based on success correlation", "auto"),
        ("fs-run-003", 8, 1, "Minor threshold adjustment for early termination", "auto"),
        ("fs-run-004", 20, 7, "Major rubric overhaul based on 20-run sample", "manual"),
        ("fs-run-005", 12, 2, "Lesson dedup and effectiveness scoring", "auto"),
    ]
    conn.executemany(
        "INSERT INTO forgesmith_runs (run_id, agent_runs_analyzed, changes_made, summary, mode) "
        "VALUES (?, ?, ?, ?, ?)", fs_runs,
    )

    # -- ForgeSmith Changes (8) ---------------------------------------
    changes = [
        ("fs-run-001", "prompt_edit", "prompts/developer.md", "Old prompt v1", "New prompt v2",
         "Success rate improved 15% with clearer instructions", '{"runs": [1,2,3]}', 0.82),
        ("fs-run-001", "weight_adjust", "forgesmith_config.json", "0.5", "0.7",
         "Code quality weight correlated with success", '{"correlation": 0.73}', 0.73),
        ("fs-run-002", "threshold_change", "dispatch_config.json", "45", "35",
         "Lower turn limit reduces timeout rate", '{"timeout_rate": "12% -> 5%"}', 0.88),
        ("fs-run-002", "prompt_edit", "prompts/tester.md", "Old tester v1", "New tester v2",
         "Added explicit test structure guidelines", '{"runs": [5,6,7]}', 0.79),
        ("fs-run-003", "lesson_merge", None, None, None,
         "Merged 3 duplicate file_not_found lessons", '{"merged_ids": [1,7,12]}', None),
        ("fs-run-004", "rubric_overhaul", "forgesmith_config.json", "v1 rubric", "v2 rubric",
         "Complete rubric rewrite with 5 new criteria", '{"sample_size": 20}', 0.91),
        ("fs-run-004", "prompt_edit", "prompts/code-reviewer.md", "Old CR v1", "New CR v2",
         "Added security-focused review checklist", '{"runs": [15,16,17]}', 0.85),
        ("fs-run-005", "effectiveness_score", None, None, None,
         "Scored all lessons by injection success rate", '{"scored": 10}', None),
    ]
    conn.executemany(
        "INSERT INTO forgesmith_changes (run_id, change_type, target_file, old_value, "
        "new_value, rationale, evidence, effectiveness_score) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", changes,
    )

    # -- Rubric Scores (15) -------------------------------------------
    rubric_scores = []
    for i in range(15):
        run_id = (i % 50) + 1
        task_id = (i % 20) + 1
        project_id = (i % 5) + 1
        role = roles[i % len(roles)]
        total = round(6.0 + (i * 0.3) % 4, 1)
        max_possible = 10.0
        normalized = round(total / max_possible, 3)
        criteria = f'{{"code_quality": {round(total * 0.4, 1)}, "completeness": {round(total * 0.3, 1)}, "style": {round(total * 0.3, 1)}}}'
        rubric_scores.append((run_id, task_id, project_id, role, 1,
                              criteria, total, max_possible, normalized))
    conn.executemany(
        "INSERT INTO rubric_scores (agent_run_id, task_id, project_id, role, "
        "rubric_version, criteria_scores, total_score, max_possible, normalized_score) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rubric_scores,
    )

    # -- Rubric Evolution History (6) ---------------------------------
    evolution = [
        (1, "developer", "code_quality", 0.4, 0.5, 0.73, 25, 10),
        (1, "developer", "completeness", 0.3, 0.25, 0.61, 25, 10),
        (1, "developer", "style", 0.3, 0.25, 0.45, 25, 10),
        (1, "tester", "coverage", 0.5, 0.6, 0.81, 15, 8),
        (1, "tester", "edge_cases", 0.3, 0.25, 0.55, 15, 8),
        (1, "tester", "clarity", 0.2, 0.15, 0.38, 15, 8),
    ]
    conn.executemany(
        "INSERT INTO rubric_evolution_history (rubric_version, role, criterion, "
        "old_weight, new_weight, correlation, sample_size_success, sample_size_failure) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", evolution,
    )

    # -- Voice Messages (3) -------------------------------------------
    voice_msgs = [
        ("inbound", "Check on TCGKungfu barcode scanner status", "processed"),
        ("outbound", "Barcode scanner is working, testing damaged card handling next", "sent"),
        ("inbound", "Deploy DOGE-HABEAS to testnet this week", "pending"),
    ]
    conn.executemany(
        "INSERT INTO voice_messages (direction, content, status) VALUES (?, ?, ?)",
        voice_msgs,
    )

    # -- API Keys (2) -------------------------------------------------
    api_keys = [
        ("anthropic", "Claude API (Production)", "sk-ant-FAKE-benchmark-key-001",
         "Primary production key", 1),
        ("openai", "GPT-4o (Backup)", "sk-FAKE-benchmark-key-002",
         "Fallback for non-critical tasks", 0),
    ]
    conn.executemany(
        "INSERT INTO api_keys (provider, label, api_key, notes, active) "
        "VALUES (?, ?, ?, ?, ?)", api_keys,
    )

    conn.commit()


def _insert_v3_data(conn):
    """Add v3-specific sample data on top of v2 data.

    Creates: 30 agent messages, 100 agent actions.
    """
    # -- Agent Messages (30) ------------------------------------------
    msg_types = ["test_results", "blocker_update", "code_notes", "security_flag"]
    messages = []
    for i in range(30):
        task_id = (i % 20) + 1
        cycle = (i // 5) + 1
        from_role = ["developer", "tester", "code-reviewer"][i % 3]
        to_role = ["tester", "developer", "developer"][i % 3]
        mtype = msg_types[i % len(msg_types)]
        content = f'{{"type": "{mtype}", "detail": "Message {i+1} content"}}'
        read = cycle + 1 if i % 4 != 0 else None
        messages.append((task_id, cycle, from_role, to_role, mtype, content, read))
    conn.executemany(
        "INSERT INTO agent_messages (task_id, cycle_number, from_role, to_role, "
        "message_type, content, read_by_cycle) VALUES (?, ?, ?, ?, ?, ?, ?)",
        messages,
    )

    # -- Agent Actions (100) ------------------------------------------
    tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep",
             "WebFetch", "TaskCreate", "TaskUpdate"]
    actions = []
    for i in range(100):
        task_id = (i % 20) + 1
        run_id = (i % 50) + 1
        cycle = (i // 10) + 1
        role = ["developer", "tester", "code-reviewer", "debugger"][i % 4]
        turn = (i % 15) + 1
        tool = tools[i % len(tools)]
        preview = f"{tool}({{'path': '/some/file_{i}.py'}})"[:200]
        inp_hash = hashlib.sha256(f"input-{i}".encode()).hexdigest()
        out_len = 100 + (i * 37) % 5000
        success = 1 if i % 12 != 0 else 0
        err_type = None if success else ["timeout", "file_not_found", "permission"][i % 3]
        err_summary = None if success else f"Error in action {i+1}"
        duration = 50 + (i * 13) % 3000
        actions.append((task_id, run_id, cycle, role, turn, tool, preview,
                        inp_hash, out_len, success, err_type, err_summary, duration))
    conn.executemany(
        "INSERT INTO agent_actions (task_id, run_id, cycle_number, role, turn_number, "
        "tool_name, tool_input_preview, input_hash, output_length, success, "
        "error_type, error_summary, duration_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", actions,
    )

    conn.commit()


# ============================================================
# Database factory: create temp DBs at each version
# ============================================================

def create_v1_database(path):
    """Create a v1 database with 19 tables and realistic data."""
    conn = sqlite3.connect(str(path))
    conn.executescript(V1_TABLES_DDL)
    _insert_v1_data(conn)
    conn.close()


def create_v2_database(path):
    """Create a v2 database with 28 tables and realistic data."""
    # Start from v1
    create_v1_database(path)
    conn = sqlite3.connect(str(path))

    # Apply v2 schema changes (same as db_migrate.migrate_v1_to_v2)
    from db_migrate import migrate_v1_to_v2
    migrate_v1_to_v2(conn)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()

    # Insert v2 data
    _insert_v2_data(conn)
    conn.close()


def create_v3_database(path):
    """Create a v3 database with 30 tables and realistic data."""
    # Start from v2
    create_v2_database(path)
    conn = sqlite3.connect(str(path))

    # Apply v3 schema changes (same as db_migrate.migrate_v2_to_v3)
    from db_migrate import migrate_v2_to_v3
    migrate_v2_to_v3(conn)
    conn.execute("PRAGMA user_version = 3")
    conn.commit()

    # Insert v3 data
    _insert_v3_data(conn)
    conn.close()


def create_fresh_database(path):
    """Create a fresh database from schema.sql (the real install path).

    This is how equipa_setup.py creates new installations: execute the
    full schema.sql which creates all 30 tables and sets user_version=3.
    Running migrations afterward should be a no-op.
    """
    schema_path = Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(path))
    conn.executescript(schema_sql)
    conn.close()


# ============================================================
# Verification helpers
# ============================================================

def get_table_names(conn):
    """Return a sorted list of user table names (excludes internal/audit tables)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def count_all_rows(conn):
    """Return {table_name: row_count} for every user table."""
    counts = {}
    for table in get_table_names(conn):
        count = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
        counts[table] = count
    return counts


def spot_check_data(conn):
    """Spot-check specific rows to verify data integrity.

    Returns a list of (check_description, passed) tuples.
    """
    checks = []

    # Check project names survived
    row = conn.execute(
        "SELECT codename FROM projects WHERE id = 1"
    ).fetchone()
    checks.append(("projects[1].codename = 'TheForge'", row and row[0] == "TheForge"))

    row = conn.execute(
        "SELECT codename FROM projects WHERE id = 3"
    ).fetchone()
    checks.append(("projects[3].codename = 'Loom'", row and row[0] == "Loom"))

    # Check task content
    row = conn.execute(
        "SELECT title FROM tasks WHERE id = 1"
    ).fetchone()
    checks.append(("tasks[1].title = 'Set up MCP server'", row and row[0] == "Set up MCP server"))

    # Check decision rationale
    row = conn.execute(
        "SELECT rationale FROM decisions WHERE id = 1"
    ).fetchone()
    checks.append((
        "decisions[1].rationale preserved",
        row and "Single-file portability" in row[0],
    ))

    # Check question content
    row = conn.execute(
        "SELECT question FROM open_questions WHERE id = 2"
    ).fetchone()
    checks.append((
        "open_questions[2] about offline kiosk",
        row and "offline kiosk" in row[0],
    ))

    return checks


def verify_backup(backup_path, pre_migration_counts):
    """Verify a backup file is valid and matches the pre-migration state.

    Returns a list of (check_description, passed) tuples.
    """
    checks = []

    # Backup file exists
    exists = backup_path.exists()
    checks.append(("Backup file exists", exists))

    if not exists:
        checks.append(("Backup is valid SQLite", False))
        checks.append(("Backup matches pre-migration state", False))
        return checks

    # Backup is valid SQLite
    try:
        backup_conn = sqlite3.connect(str(backup_path))
        backup_conn.execute("SELECT COUNT(*) FROM sqlite_master")
        valid = True
    except sqlite3.DatabaseError:
        valid = False
        backup_conn = None
    checks.append(("Backup is valid SQLite", valid))

    if not valid or backup_conn is None:
        checks.append(("Backup matches pre-migration state", False))
        return checks

    # Backup row counts match pre-migration state
    backup_counts = count_all_rows(backup_conn)
    backup_conn.close()
    match = True
    for table, expected in pre_migration_counts.items():
        actual = backup_counts.get(table, -1)
        if actual != expected:
            match = False
            break
    checks.append(("Backup matches pre-migration state", match))

    return checks


# ============================================================
# Benchmark runner
# ============================================================

class BenchmarkResult:
    """Stores results for a single migration path benchmark."""

    def __init__(self, label, from_ver, to_ver):
        self.label = label
        self.from_ver = from_ver
        self.to_ver = to_ver
        self.time_ms = 0.0
        self.tables_before = 0
        self.tables_after = 0
        self.counts_before = {}
        self.counts_after = {}
        self.data_loss = "N/A"
        self.spot_checks = []
        self.backup_checks = []
        self.pragma_ok = False
        self.audit_ok = False
        self.success = False
        self.new_tables_clean = True


def run_benchmark(label, from_ver, to_ver, create_fn):
    """Run a single migration benchmark.

    Creates a temp DB using create_fn, captures pre-migration state,
    runs the migration, and verifies everything.
    """
    result = BenchmarkResult(label, from_ver, to_ver)

    # Create temp DB
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix=f"equipa_bench_v{from_ver}_")
    tmp_path = Path(tmp.name)
    tmp.close()

    backup_path = None

    try:
        # Create the database at the starting version
        create_fn(tmp_path)

        # Capture pre-migration state
        conn = sqlite3.connect(str(tmp_path))
        result.counts_before = count_all_rows(conn)
        tables_before = get_table_names(conn)
        # Exclude schema_migrations from the "user table" count
        user_tables_before = [t for t in tables_before if t != "schema_migrations"]
        result.tables_before = len(user_tables_before)
        conn.close()

        # For v0 (empty), run_migrations needs the file to exist.
        # For v3 (no-op), it should return immediately.

        # Time the migration
        start = time.perf_counter()
        success, migrated_from, migrated_to = run_migrations(str(tmp_path), silent=True)
        elapsed = time.perf_counter() - start
        result.time_ms = elapsed * 1000
        result.success = success

        # Capture post-migration state
        conn = sqlite3.connect(str(tmp_path))
        result.counts_after = count_all_rows(conn)
        tables_after = get_table_names(conn)
        user_tables_after = [t for t in tables_after if t != "schema_migrations"]
        result.tables_after = len(user_tables_after)

        # Verify PRAGMA user_version = CURRENT_VERSION
        pv = conn.execute("PRAGMA user_version").fetchone()[0]
        result.pragma_ok = (pv == CURRENT_VERSION)

        # Verify schema_migrations audit log
        if "schema_migrations" in tables_after:
            migrations = conn.execute(
                "SELECT from_version, to_version FROM schema_migrations ORDER BY id"
            ).fetchall()
            if from_ver < CURRENT_VERSION:
                # Should have entries from each step
                expected_steps = list(range(from_ver, CURRENT_VERSION))
                actual_froms = [m[0] for m in migrations]
                # For the steps we care about (the last N entries)
                num_expected = CURRENT_VERSION - from_ver
                recent = migrations[-num_expected:] if len(migrations) >= num_expected else migrations
                recent_froms = [m[0] for m in recent]
                recent_tos = [m[1] for m in recent]
                result.audit_ok = (recent_froms == expected_steps and
                                   recent_tos == list(range(from_ver + 1, CURRENT_VERSION + 1)))
            else:
                # No-op: no new migrations logged
                result.audit_ok = True
        else:
            result.audit_ok = (from_ver >= CURRENT_VERSION)  # No-op is fine without table

        # Verify zero data loss for upgrades with existing data
        total_rows_before = sum(result.counts_before.values())
        if total_rows_before > 0:
            loss_detected = False
            for table, count in result.counts_before.items():
                after_count = result.counts_after.get(table, 0)
                if after_count < count:
                    loss_detected = True
                    break
            result.data_loss = "LOSS DETECTED" if loss_detected else "ZERO"

            # Spot-check specific rows
            result.spot_checks = spot_check_data(conn)

            # Verify new tables are empty (no phantom data)
            new_table_names = set(user_tables_after) - set(user_tables_before)
            for nt in new_table_names:
                if result.counts_after.get(nt, 0) > 0:
                    result.new_tables_clean = False
                    break

        conn.close()

        # Find and verify backup
        if from_ver > 0 and from_ver < CURRENT_VERSION:
            # Look for the backup file created by run_migrations
            parent = tmp_path.parent
            stem = tmp_path.stem
            backups = sorted(parent.glob(f"{stem}_backup_*{tmp_path.suffix}"))
            if backups:
                backup_path = backups[-1]  # Most recent
                result.backup_checks = verify_backup(backup_path, result.counts_before)
            else:
                result.backup_checks = [
                    ("Backup file exists", False),
                    ("Backup is valid SQLite", False),
                    ("Backup matches pre-migration state", False),
                ]

    finally:
        # Clean up temp files
        if tmp_path.exists():
            tmp_path.unlink()
        if backup_path and backup_path.exists():
            backup_path.unlink()

    return result


# ============================================================
# Report formatting
# ============================================================

def format_check(passed):
    """Return a pass/fail indicator."""
    return "PASS" if passed else "FAIL"


def print_report(results):
    """Print a formatted benchmark report."""
    width = 68

    print()
    print("=" * width)
    print("  EQUIPA Migration Benchmark".center(width))
    print("=" * width)
    print()

    # Summary table header
    hdr = f"  {'Upgrade Path':<22} {'Time (ms)':>10} {'Tables Before -> After':>24} {'Data Loss':>10}"
    print(hdr)
    print("  " + "-" * (width - 4))

    all_passed = True

    for r in results:
        label = r.label
        time_str = f"{r.time_ms:.1f}ms"
        tables_str = f"{r.tables_before} -> {r.tables_after}"
        loss_str = r.data_loss

        # Determine the status symbol
        checks_ok = r.success and r.pragma_ok and r.audit_ok and r.new_tables_clean
        if r.spot_checks:
            checks_ok = checks_ok and all(c[1] for c in r.spot_checks)
        if r.backup_checks:
            checks_ok = checks_ok and all(c[1] for c in r.backup_checks)

        if not checks_ok:
            all_passed = False

        status = "ok" if checks_ok else "FAIL"
        print(f"  {label:<22} {time_str:>10} {tables_str:>24} {loss_str:>7}  {status}")

    print()

    # Data preservation detail for migration paths that actually upgrade
    upgrade_results = [r for r in results
                       if r.counts_before and sum(r.counts_before.values()) > 0
                       and r.from_ver < r.to_ver]
    for r in upgrade_results:
        print(f"  Data Preservation Detail ({r.label}):")
        total_before = 0
        total_after = 0
        for table in sorted(r.counts_before.keys()):
            before = r.counts_before[table]
            after = r.counts_after.get(table, 0)
            if before > 0:
                total_before += before
                total_after += after
                preserved = "ok" if after >= before else "LOST"
                print(f"    {table + ':':<30} {before:>3} rows preserved  {preserved}")
        print(f"    {'TOTAL:':<30} {total_before:>3} rows -> {total_after} rows")
        print()

    # Condensed summary for no-op/fresh paths
    noop_results = [r for r in results
                    if r.counts_before and sum(r.counts_before.values()) > 0
                    and r.from_ver >= r.to_ver]
    if noop_results:
        print(f"  No-Op Data Verification:")
        for r in noop_results:
            total = sum(r.counts_before.values())
            after = sum(r.counts_after.values())
            status = "ok" if after >= total else "LOST"
            print(f"    {r.label}: {total} rows across {r.tables_before} tables preserved  {status}")
        print()

    # Spot check detail (use v1 -> v3 since it's the most interesting upgrade)
    v1_result = next((r for r in results if r.from_ver == 1), None)
    if v1_result and v1_result.spot_checks:
        print(f"  Spot Check Verification (v1 -> v3):")
        for desc, passed in v1_result.spot_checks:
            print(f"    {desc:<50} {format_check(passed)}")
        print()

    # New table cleanliness
    any_upgrade = [r for r in results if r.from_ver > 0 and r.from_ver < CURRENT_VERSION]
    if any_upgrade:
        print(f"  New Table Verification:")
        for r in any_upgrade:
            clean = "ok" if r.new_tables_clean else "PHANTOM DATA"
            print(f"    {r.label}: new tables are empty (no phantom data)   {clean}")
        print()

    # PRAGMA and audit verification
    print(f"  Schema Verification:")
    for r in results:
        pragma_str = format_check(r.pragma_ok)
        audit_str = format_check(r.audit_ok)
        print(f"    {r.label}: PRAGMA user_version = {CURRENT_VERSION}  {pragma_str}  |  audit log  {audit_str}")
    print()

    # Backup verification (only for migrations that create backups)
    backup_results = [r for r in results if r.backup_checks]
    if backup_results:
        print(f"  Backup Verification:")
        for r in backup_results:
            print(f"    {r.label}:")
            for desc, passed in r.backup_checks:
                print(f"      {desc:<42} {format_check(passed)}")
        print()

    # Final verdict
    print("  " + "-" * (width - 4))
    if all_passed:
        print("  All benchmarks passed. Zero data loss confirmed.")
    else:
        print("  SOME CHECKS FAILED. Review output above.")
    print()

    return all_passed


# ============================================================
# Main
# ============================================================

def main():
    """Run all migration benchmarks and print the report."""
    print()
    print("EQUIPA Migration Benchmark")
    print(f"Target schema version: v{CURRENT_VERSION}")
    print(f"Running from: {Path(__file__).parent}")
    print()

    benchmarks = [
        ("v1 -> v3", 1, CURRENT_VERSION, create_v1_database),
        ("v2 -> v3", 2, CURRENT_VERSION, create_v2_database),
        (f"v3 -> v3 (no-op)", CURRENT_VERSION, CURRENT_VERSION, create_v3_database),
        ("Fresh install", CURRENT_VERSION, CURRENT_VERSION, create_fresh_database),
    ]

    results = []
    for label, from_ver, to_ver, factory in benchmarks:
        print(f"  Running: {label} ...", end="", flush=True)
        result = run_benchmark(label, from_ver, to_ver, factory)
        print(f" {result.time_ms:.1f}ms")
        results.append(result)

    all_passed = print_report(results)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

