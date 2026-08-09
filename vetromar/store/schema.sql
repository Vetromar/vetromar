-- Vetromar store v3: graph-shaped, bi-temporal, provenance-carrying knowledge
-- store over SQLite. Typed nodes (units, entities) + episodes (raw layer) +
-- typed edges. `payload` columns hold the full model JSON (source of truth);
-- the other columns are denormalized copies for filtering/indexing.

CREATE TABLE IF NOT EXISTS episodes (
    id          TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,    -- 'meeting', 'email_thread', 'chat', 'document', ... (open)
    title       TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    raw         TEXT,             -- canonical raw content (evidence is validated against this)
    raw_ref     TEXT,             -- pointer to big binaries / the external source
    external_id TEXT              -- stable id at the source ('slack:C123:1721...'); NULL for capture/manual
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_external
    ON episodes(external_id) WHERE external_id IS NOT NULL;

-- Per-source sync watermarks for the MCP-client pull loop. `cursor` is opaque
-- JSON owned by the sync agent (sources differ: timestamps, tokens, per-channel
-- maps) — the store only persists it.
CREATE TABLE IF NOT EXISTS sync_state (
    source         TEXT PRIMARY KEY,
    cursor         TEXT NOT NULL,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,    -- payload kind: decision/claim/commitment/question/metric
    status      TEXT CHECK (status IN ('Decided', 'Leaning', 'Parked')),  -- decisions only; NULL otherwise
    episode_id  TEXT NOT NULL REFERENCES episodes(id),
    method      TEXT NOT NULL,    -- provenance method: captured/concierge/pushed/derived
    valid_from  TEXT NOT NULL,
    valid_to    TEXT,             -- NULL while the unit is current
    ingested_at TEXT NOT NULL,
    payload     TEXT NOT NULL     -- full Unit JSON (single source of truth)
);

CREATE INDEX IF NOT EXISTS idx_units_type     ON units(type);
CREATE INDEX IF NOT EXISTS idx_units_status   ON units(status);
CREATE INDEX IF NOT EXISTS idx_units_episode  ON units(episode_id);
CREATE INDEX IF NOT EXISTS idx_units_valid    ON units(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_units_ingested ON units(ingested_at);

CREATE TABLE IF NOT EXISTS entities (
    id      TEXT PRIMARY KEY,
    type    TEXT NOT NULL,        -- 'person', 'project', 'product', 'team', ... (open)
    name    TEXT NOT NULL,
    payload TEXT NOT NULL         -- full Entity JSON (aliases live here)
);

CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

CREATE TABLE IF NOT EXISTS edges (
    id         TEXT PRIMARY KEY,
    from_id    TEXT NOT NULL,     -- unit_* or ent_* (polymorphic — app-validated, no FK)
    to_id      TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'related',
    method     TEXT NOT NULL DEFAULT 'manual',  -- manual/auto-exact/auto-fuzzy/auto-embed/auto-llm
    confidence REAL,              -- NULL = human-made
    ref        TEXT,              -- verbatim mention string (mentions/about edges)
    rationale  TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (from_id, to_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to   ON edges(to_id);

CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(
    unit_id UNINDEXED,
    text
);

CREATE TABLE IF NOT EXISTS unit_vectors (
    unit_id   TEXT PRIMARY KEY,
    embedding BLOB NOT NULL       -- float32 little-endian, written by the search layer
);

-- Workspace replication (M14). Every knowledge write appends to `changelog`
-- (the push outbox) in the same transaction; derived tables (units_fts,
-- unit_vectors) and per-source sync_state are never replicated.
CREATE TABLE IF NOT EXISTS changelog (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id   TEXT NOT NULL UNIQUE,
    table_name  TEXT NOT NULL,    -- episodes/units/entities/edges
    op          TEXT NOT NULL,    -- 'insert' | 'update' (supersede, alias merge)
    row_id      TEXT NOT NULL,
    payload     TEXT NOT NULL,    -- full model JSON after the write
    recorded_at TEXT NOT NULL,
    pushed      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_changelog_pending ON changelog(pushed, seq);

-- Pull cursor, bootstrap flag, and other sync bookkeeping.
CREATE TABLE IF NOT EXISTS replication_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Remote changes that could not be applied (gate failure, missing episode/
-- node). Visible-not-silent divergence: surfaced in the Workspace tab and
-- retried before every pull.
CREATE TABLE IF NOT EXISTS replication_quarantine (
    change_id   TEXT PRIMARY KEY,
    envelope    TEXT NOT NULL,    -- full server change envelope JSON
    reason      TEXT NOT NULL,
    received_at TEXT NOT NULL
);

PRAGMA user_version = 4;
