PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS indicators (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL UNIQUE,
    unit        TEXT NOT NULL DEFAULT 'MPa',
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL UNIQUE,
    specimen_header  TEXT NOT NULL,
    revision_header  TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS template_columns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id  INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
    header       TEXT NOT NULL,
    indicator_id INTEGER NOT NULL REFERENCES indicators(id),
    UNIQUE (template_id, header),
    UNIQUE (template_id, indicator_id)
);

CREATE TABLE IF NOT EXISTS specimens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    number     TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    filename             TEXT NOT NULL,
    content              BLOB NOT NULL,
    content_sha256       TEXT NOT NULL,
    size_bytes           INTEGER NOT NULL,
    uploaded_at          TEXT NOT NULL,
    template_id          INTEGER NOT NULL REFERENCES templates(id),
    original_template_id INTEGER NOT NULL REFERENCES templates(id),
    template_changed_at  TEXT,
    parse_notes          TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS declarations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    specimen_id  INTEGER NOT NULL REFERENCES specimens(id),
    indicator_id INTEGER NOT NULL REFERENCES indicators(id),
    revision     INTEGER NOT NULL,
    revision_raw TEXT NOT NULL,
    value_micro  INTEGER NOT NULL,
    row_number   INTEGER,
    UNIQUE (batch_id, specimen_id, indicator_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_declarations_key
    ON declarations (specimen_id, indicator_id, revision, value_micro);
CREATE INDEX IF NOT EXISTS idx_declarations_batch
    ON declarations (batch_id);

CREATE TABLE IF NOT EXISTS base_values (
    specimen_id  INTEGER NOT NULL REFERENCES specimens(id),
    indicator_id INTEGER NOT NULL REFERENCES indicators(id),
    revision     INTEGER NOT NULL,
    revision_raw TEXT NOT NULL,
    value_micro  INTEGER NOT NULL,
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (specimen_id, indicator_id)
);

CREATE TABLE IF NOT EXISTS batch_template_changes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    from_template_id INTEGER NOT NULL REFERENCES templates(id),
    to_template_id   INTEGER NOT NULL REFERENCES templates(id),
    changed_at       TEXT NOT NULL
);
