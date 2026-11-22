"""Command line helpers: ``flask --app warehouse init-db`` / ``seed-demo``."""

from __future__ import annotations

import sqlite3

import click
from flask import Flask
from flask.cli import with_appcontext

from . import core
from .db import get_db, init_db

INDICATORS = [
    ("STRENGTH_MEASURED", "抗压强度（实测）", "试件实测抗压强度"),
    ("STRENGTH_CONVERTED", "抗压强度（折算）", "按标准条件折算后的抗压强度"),
    ("SPLIT_TENSILE", "劈裂抗拉强度", "劈裂法测得的抗拉强度"),
]

TEMPLATES = [
    (
        "常规抗压报告",
        "试件编号",
        "报告修订号",
        [("强度", "STRENGTH_MEASURED"), ("劈裂抗拉强度", "SPLIT_TENSILE")],
    ),
    (
        "折算强度报告",
        "试件编号",
        "报告修订号",
        [("强度", "STRENGTH_CONVERTED")],
    ),
    (
        "补页（仅强度）",
        "试件编号",
        "报告修订号",
        [("强度", "STRENGTH_MEASURED")],
    ),
]

DEMO_BATCHES = [
    (
        "常规抗压报告",
        "demo-常规抗压报告-首报.csv",
        "试件编号,报告修订号,强度,劈裂抗拉强度\n"
        "C-001,1,32.4,2.81\n"
        "C-002,1,35.1,3.02\n",
    ),
    (
        "补页（仅强度）",
        "demo-补页-修订2.csv",
        "试件编号,报告修订号,强度,备注\n"
        "C-001,2,33.0,复检\n",
    ),
    (
        "折算强度报告",
        "demo-折算强度-误选模板.csv",
        "试件编号,报告修订号,强度\n"
        "C-003,1,28.7\n",
    ),
]


def register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    @with_appcontext
    def init_db_command() -> None:
        """Create the schema in the configured database."""
        init_db(get_db())
        click.echo("数据库结构已初始化。")

    @app.cli.command("seed-demo")
    @with_appcontext
    def seed_demo_command() -> None:
        """Insert indicator/template metadata and a few example batches."""
        conn = get_db()
        init_db(conn)
        _seed_metadata(conn)
        _seed_batches(conn)
        click.echo("演示数据已写入。")

    @app.cli.command("seed-metadata")
    @with_appcontext
    def seed_metadata_command() -> None:
        """Insert only the indicator and template metadata."""
        conn = get_db()
        init_db(conn)
        _seed_metadata(conn)
        click.echo("指标与模板元数据已写入。")


def _seed_metadata(conn: sqlite3.Connection) -> None:
    for code, name, description in INDICATORS:
        conn.execute(
            "INSERT OR IGNORE INTO indicators (code, name, unit, description, created_at)"
            " VALUES (?, ?, 'MPa', ?, ?)",
            (code, name, description, core.now_iso()),
        )
    indicator_ids = {
        row["code"]: row["id"]
        for row in conn.execute("SELECT id, code FROM indicators")
    }
    for name, specimen_header, revision_header, columns in TEMPLATES:
        existing = conn.execute(
            "SELECT id FROM templates WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            continue
        cursor = conn.execute(
            "INSERT INTO templates (name, specimen_header, revision_header, created_at)"
            " VALUES (?, ?, ?, ?)",
            (name, specimen_header, revision_header, core.now_iso()),
        )
        for header, code in columns:
            conn.execute(
                "INSERT INTO template_columns (template_id, header, indicator_id)"
                " VALUES (?, ?, ?)",
                (cursor.lastrowid, header, indicator_ids[code]),
            )
    conn.commit()


def _seed_batches(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]:
        return
    for template_name, filename, text in DEMO_BATCHES:
        template = conn.execute(
            "SELECT * FROM templates WHERE name = ?", (template_name,)
        ).fetchone()
        content = text.encode("utf-8")
        parsed = core.parse_csv(conn, template, content)
        conflicts = core.find_conflicts(conn, parsed.rows, subject_label="演示数据")
        if conflicts:  # pragma: no cover - demo data is fixed
            raise click.ClickException(f"演示批次 {filename} 存在冲突")
        core.create_batch(
            conn,
            filename=filename,
            content=content,
            template_id=template["id"],
            parsed=parsed,
        )
    conn.commit()
