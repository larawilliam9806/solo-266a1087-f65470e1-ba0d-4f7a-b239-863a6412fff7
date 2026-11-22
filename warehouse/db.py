"""Database connection and schema bootstrap."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = current_app.config["DATABASE"]
        parent = Path(db_path).parent
        parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        g.db = conn
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    if own:
        conn = get_db()
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    if own:
        conn.commit()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    database = app.config["DATABASE"]
    if app.config.get("AUTO_INIT_DB", True):
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(database, timeout=30)
        try:
            raw.execute("PRAGMA journal_mode = WAL")
            init_db(raw)
        except sqlite3.OperationalError:
            # A second worker may be creating the tables at the same moment
            # (``CREATE TABLE IF NOT EXISTS`` is not free of that race);
            # never fail startup because of it.
            if not raw.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'batches'"
            ).fetchone():
                raise
        finally:
            raw.close()


def database_path(app) -> str:
    return app.config["DATABASE"]
