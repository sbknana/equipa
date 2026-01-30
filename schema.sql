-- Itzamna Database Schema
-- Canonical DDL for creating a fresh Itzamna/ForgeTeam database.
-- Generated from the live theforge.db schema.
-- Used by itzamna_setup.py to create new installations.
--
-- Tables: 19, Views: 5, Triggers: 1, Indexes: 7

-- ============================================================
-- TABLES
-- ============================================================

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

-- ============================================================
-- INDEXES
-- ============================================================

CREATE INDEX idx_tasks_project_status ON tasks(project_id, status);
CREATE INDEX idx_decisions_project ON decisions(project_id);
CREATE INDEX idx_open_questions_resolved ON open_questions(resolved);
CREATE INDEX idx_projects_status ON projects(status);
CREATE INDEX idx_components_project ON components(project_id);
CREATE INDEX idx_xref_source ON cross_references(source_table, source_id);
CREATE INDEX idx_xref_target ON cross_references(target_table, target_id);

-- ============================================================
-- TRIGGERS
-- ============================================================

CREATE TRIGGER update_project_timestamp
AFTER UPDATE ON projects
BEGIN
    UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

-- ============================================================
-- VIEWS
-- ============================================================

CREATE VIEW v_project_dashboard AS
SELECT
    p.id,
    p.codename,
    p.status,
    (SELECT COUNT(*) FROM tasks WHERE project_id = p.id AND status = 'todo') as todo_count,
    (SELECT COUNT(*) FROM tasks WHERE project_id = p.id AND status = 'in_progress') as in_progress_count,
    (SELECT COUNT(*) FROM tasks WHERE project_id = p.id AND status = 'blocked') as blocked_count,
    (SELECT COUNT(*) FROM open_questions WHERE project_id = p.id AND resolved = 0) as open_questions,
    (SELECT MAX(session_date) FROM session_notes WHERE project_id = p.id) as last_session
FROM projects p
WHERE p.status = 'active';

CREATE VIEW v_stale_tasks AS
SELECT t.*, p.codename as project_name,
       julianday('now') - julianday(t.created_at) as days_stale
FROM tasks t
JOIN projects p ON t.project_id = p.id
WHERE t.status = 'in_progress'
  AND julianday('now') - julianday(t.created_at) > 3;

CREATE VIEW v_stale_questions AS
SELECT q.*, p.codename as project_name,
       julianday('now') - julianday(q.asked_at) as days_open
FROM open_questions q
JOIN projects p ON q.project_id = p.id
WHERE q.resolved = 0
  AND julianday('now') - julianday(q.asked_at) > 7;

CREATE VIEW v_upcoming_reminders AS
SELECT r.*, p.codename as project_name,
       julianday(r.reminder_date) - julianday('now') as days_until
FROM reminders r
LEFT JOIN projects p ON r.project_id = p.id
WHERE r.status = 'pending'
  AND julianday(r.reminder_date) - julianday('now') <= 7
ORDER BY r.reminder_date;

CREATE VIEW v_content_alerts AS
SELECT * FROM content_tickler
WHERE needs_content = 1
   OR posts_remaining <= alert_threshold;
