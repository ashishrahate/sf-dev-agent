-- Metadata index schema.
--
-- Design: every metadata item — ApexClass, ApexTrigger, CustomObject, CustomField,
-- ValidationRule, Flow, CustomMetadataType, LWC, etc. — is stored as a row in the
-- `components` table. Type-specific extracted fields live in `metadata_json`, so
-- adding a new component type requires only a new parser, never a schema migration.
--
-- Relationships (trigger->object, field->object, class extends class, flow->object,
-- approval-process->object) are stored as edges in `relationships` with a string
-- relationship_type, again no DDL change needed for new edge kinds.

CREATE TABLE IF NOT EXISTS components (
    id                    TEXT PRIMARY KEY,           -- "ApexClass:AccountHandler"
    component_type        TEXT NOT NULL,              -- "ApexClass", "ApexTrigger", "CustomObject", ...
    api_name              TEXT NOT NULL,              -- "AccountHandler"
    parent_id             TEXT,                       -- e.g. CustomField -> CustomObject:Account
    file_path             TEXT,                       -- relative path inside retrieve dir
    source                TEXT,                       -- raw .cls / .trigger / xml content
    metadata_json         TEXT NOT NULL DEFAULT '{}', -- type-specific extracted fields
    last_indexed_at       TEXT NOT NULL,              -- ISO-8601 UTC
    embedding             BLOB,                       -- float32 vector serialized via .tobytes()
    embedded_source_hash  TEXT,                       -- sha256 of the text we embedded; gate for re-embed
    FOREIGN KEY (parent_id) REFERENCES components(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_components_type      ON components(component_type);
CREATE INDEX IF NOT EXISTS idx_components_api_name  ON components(api_name);
CREATE INDEX IF NOT EXISTS idx_components_parent    ON components(parent_id);

CREATE TABLE IF NOT EXISTS relationships (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id         TEXT NOT NULL,            -- component initiating the relationship
    target_id         TEXT NOT NULL,            -- component on the receiving end
    relationship_type TEXT NOT NULL,            -- "TRIGGERS_ON", "REFERENCES", "EXTENDS", ...
    metadata_json     TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_id) REFERENCES components(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES components(id) ON DELETE CASCADE,
    UNIQUE (source_id, target_id, relationship_type)
);

CREATE INDEX IF NOT EXISTS idx_relationships_source ON relationships(source_id);
CREATE INDEX IF NOT EXISTS idx_relationships_target ON relationships(target_id);
CREATE INDEX IF NOT EXISTS idx_relationships_type   ON relationships(relationship_type);

-- Index-build runs are recorded so callers can see when the index was last refreshed
-- and which component types were ingested.
CREATE TABLE IF NOT EXISTS index_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    org_alias         TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    completed_at      TEXT,
    component_types   TEXT NOT NULL DEFAULT '[]',
    components_count  INTEGER NOT NULL DEFAULT 0,
    error             TEXT
);

-- Knowledge base — Salesforce platform knowledge that's NOT org-specific.
-- Entries live as Markdown files in `knowledge/entries/<category>/*.md` and
-- are auto-ingested into this table on first KnowledgeBase open.
-- Uses the same embedding shape as `components` (BLOB + content hash gate).
CREATE TABLE IF NOT EXISTS knowledge_entries (
    id                    TEXT PRIMARY KEY,           -- e.g. "gl-soql-queries-101"
    title                 TEXT NOT NULL,
    category              TEXT NOT NULL,              -- governor_limit | anti_pattern | best_practice | pattern
    severity              TEXT,                       -- critical | high | medium | low | info
    tags_json             TEXT NOT NULL DEFAULT '[]', -- JSON array of strings
    references_json       TEXT NOT NULL DEFAULT '[]', -- JSON array of URLs
    body                  TEXT NOT NULL,              -- the Markdown body
    file_path             TEXT,                       -- path on disk we ingested from
    embedding             BLOB,                       -- float32 vector via .tobytes()
    embedded_text_hash    TEXT,                       -- sha256 of the embedded text
    last_loaded_at        TEXT NOT NULL               -- ISO-8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge_entries(category);
CREATE INDEX IF NOT EXISTS idx_knowledge_severity ON knowledge_entries(severity);

-- Memory tier (Wave 8 slice 1) — project memory, scoped per (tenant_id, org_alias).
-- Working memory is NOT in this table; conversation persistence is deferred.
-- Same embedding shape as components / knowledge_entries (BLOB + content hash).
--
-- Type taxonomy is ported from Claude Code's auto-memory:
--   user      -> facts about the human user (role, preferences, knowledge)
--   feedback  -> corrections + validated non-obvious choices
--   project   -> ongoing work, decisions, deadlines
--   reference -> pointers to external systems (dashboards, ticket projects)
--
-- `superseded_by` points at the row that replaced this one during compaction;
-- the original is kept (not deleted) for auditability.
CREATE TABLE IF NOT EXISTS memories (
    id                    TEXT PRIMARY KEY,           -- "<tenant>:<org>:<slug>" stable across runs
    tenant_id             TEXT NOT NULL,              -- multi-tenant from day one (single-tenant runtime today)
    org_alias             TEXT,                       -- NULL = applies to all orgs in the tenant
    type                  TEXT NOT NULL,              -- user | feedback | project | reference
    name                  TEXT NOT NULL,              -- short human-friendly handle
    description           TEXT NOT NULL,              -- one-line relevance hook (used at recall ranking time)
    body                  TEXT NOT NULL,              -- the memory content (rule + Why + How to apply)
    tags_json             TEXT NOT NULL DEFAULT '[]', -- JSON array of strings
    source_session_id     TEXT,                       -- provenance: which session wrote this
    created_at            TEXT NOT NULL,              -- ISO-8601 UTC
    last_accessed_at      TEXT NOT NULL,              -- bumped by recall(); used for decay (slice 2)
    access_count          INTEGER NOT NULL DEFAULT 0, -- bumped by recall()
    superseded_by         TEXT,                       -- compaction link to a newer memory.id
    embedding             BLOB,                       -- float32 vector via .tobytes()
    embedded_text_hash    TEXT,                       -- sha256 of embedded text; gates re-embed
    FOREIGN KEY (superseded_by) REFERENCES memories(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_scope     ON memories(tenant_id, org_alias);
CREATE INDEX IF NOT EXISTS idx_memories_type      ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_superseded ON memories(superseded_by);

-- Working memory (Wave 8 slice 2a) — task state + conversation transcripts.
-- Persists every Task and every message in its conversation so the agent can
-- resume from crash, review past sessions, and feed past conversations to
-- the LLM-driven extraction pipeline (slice 2c).
--
-- Each Task row mirrors `models.schemas.Task` plus an org_alias scope and
-- the JSON-serialized plan / result. `conversation_messages` stores each
-- message as (task_id, seq) with the content_json blob — content can be a
-- string or a list of typed blocks (text / tool_use / tool_result).
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,           -- the Task.task_id
    tenant_id       TEXT NOT NULL,
    org_alias       TEXT,                       -- nullable: pre-org-binding tasks
    status          TEXT NOT NULL,              -- TaskStatus enum value
    user_request    TEXT NOT NULL,              -- the original ask
    plan_json       TEXT,                       -- serialized ExecutionPlan, set when registered
    plan_approved   INTEGER NOT NULL DEFAULT 0, -- 0 or 1
    result_json     TEXT,                       -- serialized TaskResult, set on completion
    error           TEXT,                       -- failure detail
    mode            TEXT NOT NULL DEFAULT 'plan', -- AgentMode enum value (slice C)
    created_at      TEXT NOT NULL,              -- ISO-8601 UTC
    updated_at      TEXT NOT NULL,              -- bumped on every mutation
    completed_at    TEXT                        -- set when status reaches a terminal state
);

CREATE INDEX IF NOT EXISTS idx_tasks_scope  ON tasks(tenant_id, org_alias);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    seq             INTEGER NOT NULL,           -- 0-indexed; replay order
    role            TEXT NOT NULL,              -- "user" | "assistant"
    content_json    TEXT NOT NULL,              -- JSON-serialized message content
    created_at      TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    UNIQUE (task_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_msgs_task_seq ON conversation_messages(task_id, seq);
