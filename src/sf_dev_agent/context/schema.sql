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
    id              TEXT PRIMARY KEY,           -- "ApexClass:AccountHandler"
    component_type  TEXT NOT NULL,              -- "ApexClass", "ApexTrigger", "CustomObject", ...
    api_name        TEXT NOT NULL,              -- "AccountHandler"
    parent_id       TEXT,                       -- e.g. CustomField -> CustomObject:Account
    file_path       TEXT,                       -- relative path inside retrieve dir
    source          TEXT,                       -- raw .cls / .trigger / xml content
    metadata_json   TEXT NOT NULL DEFAULT '{}', -- type-specific extracted fields
    last_indexed_at TEXT NOT NULL,              -- ISO-8601 UTC
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
