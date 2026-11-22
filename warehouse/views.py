"""Flask routes for the concrete declaration warehouse."""

from __future__ import annotations

import json
import secrets
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import core
from .db import get_db

bp = Blueprint("main", __name__)


# ---------------------------------------------------------------------------
# request plumbing
# ---------------------------------------------------------------------------


@bp.app_context_processor
def inject_globals() -> dict[str, Any]:
    def csrf_token() -> str:
        token = session.get("_csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf"] = token
        return token

    return {"csrf_token": csrf_token}


@bp.app_template_filter("dt")
def format_datetime(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")


@bp.app_template_filter("micro")
def format_micro_filter(value: int | None) -> str:
    return "—" if value is None else core.format_micro(value)


@bp.before_request
def protect_post() -> None:
    if request.method != "POST" or not current_app.config.get("CSRF_ENABLED", True):
        return
    token = session.get("_csrf")
    submitted = request.form.get("_csrf", "")
    if not token or not submitted or not secrets.compare_digest(token, submitted):
        abort(400, description="表单已过期，请刷新页面后重试。")


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    """Atomic write scope so rejected operations leave no trace."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


class Rejected(Exception):
    """Raised inside a write transaction to roll back and render a message."""

    def __init__(self, status: int, template: str, payload: dict[str, Any]):
        self.status = status
        self.template = template
        self.payload = payload
        super().__init__(payload)


def _int_field(name: str) -> int | None:
    raw = request.form.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _safe_filename(raw: str) -> str:
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ord(ch) >= 32).strip()
    return name[:255] or "upload.csv"


def _normalize_header(raw: str) -> str:
    """Column names are matched after NFKC + strip, so store them that way."""
    return unicodedata.normalize("NFKC", raw or "").strip()


def _load_batch_or_404(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row:
    batch = conn.execute(
        """
        SELECT b.*, ct.name AS current_template_name,
               ot.name AS original_template_name
        FROM batches b
        LEFT JOIN templates ct ON ct.id = b.template_id
        LEFT JOIN templates ot ON ot.id = b.original_template_id
        WHERE b.id = ?
        """,
        (batch_id,),
    ).fetchone()
    if batch is None:
        abort(404, description=f"批次 #{batch_id} 不存在")
    return batch


def _batch_notes(batch: sqlite3.Row) -> dict[str, Any]:
    try:
        return json.loads(batch["parse_notes"] or "{}")
    except (TypeError, ValueError):
        return {}


def _template_options(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT t.*, COUNT(tc.id) AS column_count
        FROM templates t
        LEFT JOIN template_columns tc ON tc.template_id = t.id
        GROUP BY t.id
        ORDER BY t.name
        """
    ).fetchall()


def _batch_declarations(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT d.*, s.number AS specimen_number, i.code, i.name AS indicator_name
        FROM declarations d
        JOIN specimens s ON s.id = d.specimen_id
        JOIN indicators i ON i.id = d.indicator_id
        WHERE d.batch_id = ?
        ORDER BY s.number, i.name, d.revision
        """,
        (batch_id,),
    ).fetchall()


def _batch_changes(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.*, ft.name AS from_name, tt.name AS to_name
        FROM batch_template_changes c
        JOIN templates ft ON ft.id = c.from_template_id
        JOIN templates tt ON tt.id = c.to_template_id
        WHERE c.batch_id = ? ORDER BY c.changed_at, c.id
        """,
        (batch_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------


@bp.get("/")
def index():
    conn = get_db()
    stats = {
        "specimens": conn.execute("SELECT COUNT(*) FROM specimens").fetchone()[0],
        "batches": conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0],
        "templates": conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0],
        "indicators": conn.execute("SELECT COUNT(*) FROM indicators").fetchone()[0],
    }
    return render_template(
        "index.html",
        stats=stats,
        batches=core.list_batches(conn)[:8],
        templates=_template_options(conn),
    )


# ---------------------------------------------------------------------------
# indicators
# ---------------------------------------------------------------------------


def _indicator_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT i.*, COUNT(tc.id) AS template_count
        FROM indicators i
        LEFT JOIN template_columns tc ON tc.indicator_id = i.id
        GROUP BY i.id ORDER BY i.code
        """
    ).fetchall()


@bp.get("/indicators")
def indicators():
    return render_template(
        "indicators.html", indicators=_indicator_rows(get_db()), errors=[], form={}
    )


@bp.post("/indicators")
def create_indicator():
    conn = get_db()
    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    errors: list[str] = []
    if not code:
        errors.append("指标编码不能为空")
    elif not code.replace("-", "").replace("_", "").isalnum():
        errors.append("指标编码只能包含字母、数字、连字符和下划线")
    if not name:
        errors.append("指标名称不能为空")
    if not errors:
        try:
            with write_transaction(conn):
                conn.execute(
                    "INSERT INTO indicators (code, name, unit, description, created_at)"
                    " VALUES (?, ?, 'MPa', ?, ?)",
                    (code, name, description, core.now_iso()),
                )
        except sqlite3.IntegrityError:
            errors.append(f"指标编码「{code}」或名称「{name}」已存在")
        else:
            flash(f"已登记指标「{name}」（{code}）", "success")
            return redirect(url_for("main.indicators"))

    return (
        render_template(
            "indicators.html",
            indicators=_indicator_rows(conn),
            errors=errors,
            form={"code": code, "name": name, "description": description},
        ),
        400,
    )


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------


@bp.get("/templates")
def templates():
    conn = get_db()
    rows = conn.execute(
        "SELECT t.*, COUNT(tc.id) AS column_count FROM templates t"
        " LEFT JOIN template_columns tc ON tc.template_id = t.id"
        " GROUP BY t.id ORDER BY t.name"
    ).fetchall()
    columns: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(
        """
        SELECT tc.*, i.code, i.name, i.unit
        FROM template_columns tc JOIN indicators i ON i.id = tc.indicator_id
        ORDER BY tc.id
        """
    ):
        columns.setdefault(row["template_id"], []).append(row)
    return render_template("templates.html", templates=rows, columns=columns)


@bp.get("/templates/new")
def new_template():
    conn = get_db()
    return render_template(
        "template_form.html",
        indicators=conn.execute("SELECT * FROM indicators ORDER BY code").fetchall(),
        form={},
        mapping=[{"header": "", "indicator_id": ""} for _ in range(3)],
        errors=[],
    )


@bp.post("/templates")
def create_template():
    conn = get_db()
    name = request.form.get("name", "").strip()
    specimen_header = _normalize_header(request.form.get("specimen_header", ""))
    revision_header = _normalize_header(request.form.get("revision_header", ""))
    raw_headers = request.form.getlist("header")
    raw_indicators = request.form.getlist("indicator_id")
    mapping = [
        {"header": _normalize_header(h), "indicator_id": i.strip()}
        for h, i in zip(raw_headers, raw_indicators)
        if h.strip() or i.strip()
    ]
    errors: list[str] = []

    if not name:
        errors.append("模板名称不能为空")
    if not specimen_header:
        errors.append("试件号列名不能为空")
    if not revision_header:
        errors.append("报告修订号列名不能为空")
    if specimen_header and specimen_header == revision_header:
        errors.append("试件号列名与报告修订号列名不能相同")
    if not mapping:
        errors.append("至少登记一个指标列")

    seen_headers: set[str] = set()
    seen_indicators: set[int] = set()
    indicator_ids: list[int] = []
    for index, item in enumerate(mapping, start=1):
        header = item["header"]
        if not header:
            errors.append(f"第 {index} 个指标列缺少列名")
            indicator_ids.append(0)
            continue
        if header in (specimen_header, revision_header):
            errors.append(f"指标列名「{header}」与试件号/修订号列名重复")
        if header in seen_headers:
            errors.append(f"指标列名「{header}」重复")
        seen_headers.add(header)
        try:
            indicator_id = int(item["indicator_id"])
        except (TypeError, ValueError):
            indicator_id = 0
        if indicator_id <= 0:
            errors.append(f"指标列「{header}」未选择指标")
        elif conn.execute(
            "SELECT 1 FROM indicators WHERE id = ?", (indicator_id,)
        ).fetchone() is None:
            errors.append(f"指标列「{header}」选择了不存在的指标")
        if indicator_id > 0 and indicator_id in seen_indicators:
            errors.append(f"指标列「{header}」选择了重复的指标")
        seen_indicators.add(indicator_id)
        indicator_ids.append(indicator_id)

    if not errors:
        try:
            with write_transaction(conn):
                cursor = conn.execute(
                    "INSERT INTO templates"
                    " (name, specimen_header, revision_header, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (name, specimen_header, revision_header, core.now_iso()),
                )
                template_id = int(cursor.lastrowid)
                for item, indicator_id in zip(mapping, indicator_ids):
                    conn.execute(
                        "INSERT INTO template_columns"
                        " (template_id, header, indicator_id) VALUES (?, ?, ?)",
                        (template_id, item["header"], indicator_id),
                    )
        except sqlite3.IntegrityError:
            errors.append(f"模板名称「{name}」已存在")
        else:
            flash(f"已登记模板「{name}」", "success")
            return redirect(url_for("main.templates"))

    return (
        render_template(
            "template_form.html",
            indicators=conn.execute(
                "SELECT * FROM indicators ORDER BY code"
            ).fetchall(),
            form={
                "name": name,
                "specimen_header": specimen_header,
                "revision_header": revision_header,
            },
            mapping=mapping,
            errors=errors,
        ),
        400,
    )


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------


@bp.get("/upload")
def upload():
    conn = get_db()
    return render_template(
        "upload.html",
        templates=_template_options(conn),
        errors=[],
        selected_template=None,
    )


@bp.post("/upload")
def upload_post():
    conn = get_db()
    template_id = _int_field("template_id")
    file = request.files.get("file")
    errors: list[str] = []

    template = core.load_template(conn, template_id) if template_id else None
    if template_id is None:
        errors.append("请选择报告模板")
    elif template is None:
        errors.append("所选报告模板不存在")
    if file is None or not file.filename:
        errors.append("请选择要上传的 CSV 文件")

    filename = _safe_filename(file.filename) if file is not None and file.filename else ""
    if filename and Path(filename).suffix.lower() not in (".csv", ".txt"):
        errors.append("仅支持 .csv 或 .txt 文件")

    content = b""
    if not errors:
        content = file.read()
        if not content:
            errors.append("文件为空")

    if errors:
        return (
            render_template(
                "upload.html",
                templates=_template_options(conn),
                errors=errors,
                selected_template=template_id,
            ),
            400,
        )

    batch_id: int | None = None
    parsed: core.ParseResult | None = None
    try:
        with write_transaction(conn):
            parsed = core.parse_csv(conn, template, content)
            conflicts = core.find_conflicts(
                conn, parsed.rows, subject_label=f"本次上传「{filename}」"
            )
            if conflicts:
                raise Rejected(
                    409,
                    "conflict.html",
                    {
                        "title": "上传被拒绝：同一试件、指标和修订号出现不同值",
                        "conflicts": conflicts,
                        "back_url": url_for("main.upload"),
                        "back_label": "返回上传页",
                        "detail": "本次上传未写入任何数据，原档与底表保持原状。"
                        "请核对文件后重新上传。",
                    },
                )
            batch_id = core.create_batch(
                conn,
                filename=filename,
                content=content,
                template_id=template["id"],
                parsed=parsed,
            )
    except core.ParseError as exc:
        return (
            render_template(
                "upload.html",
                templates=_template_options(conn),
                errors=exc.issues,
                selected_template=template_id,
            ),
            400,
        )
    except Rejected as exc:
        return render_template(exc.template, **exc.payload), exc.status

    flash(
        f"批次 #{batch_id} 已入仓，共申报 {len(parsed.rows)} 个指标值。", "success"
    )
    return redirect(url_for("main.batch_detail", batch_id=batch_id))


# ---------------------------------------------------------------------------
# batches
# ---------------------------------------------------------------------------


@bp.get("/batches")
def batches():
    conn = get_db()
    return render_template("batches.html", batches=core.list_batches(conn))


@bp.get("/batches/<int:batch_id>")
def batch_detail(batch_id: int):
    conn = get_db()
    batch = _load_batch_or_404(conn, batch_id)
    return render_template(
        "batch_detail.html",
        batch=batch,
        declarations=_batch_declarations(conn, batch_id),
        changes=_batch_changes(conn, batch_id),
        notes=_batch_notes(batch),
        templates=_template_options(conn),
        errors=[],
    )


@bp.get("/batches/<int:batch_id>/download")
def batch_download(batch_id: int):
    conn = get_db()
    batch = _load_batch_or_404(conn, batch_id)
    return send_file(
        BytesIO(batch["content"]),
        mimetype="text/csv",
        as_attachment=True,
        download_name=batch["filename"],
        max_age=0,
    )


@bp.post("/batches/<int:batch_id>/switch")
def batch_switch(batch_id: int):
    conn = get_db()
    batch = _load_batch_or_404(conn, batch_id)
    new_template_id = _int_field("template_id")
    if new_template_id is None:
        abort(400, description="请选择目标模板")
    new_template = core.load_template(conn, new_template_id)
    if new_template is None:
        abort(404, description="目标模板不存在")

    if new_template_id == batch["template_id"]:
        flash("所选模板与该批次当前模板相同，未作改动。", "info")
        return redirect(url_for("main.batch_detail", batch_id=batch_id))

    changes: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    conflicts: list[dict[str, Any]] = []
    try:
        with write_transaction(conn):
            # Re-interpret the original bytes with the new template.  The
            # batch's own old declarations are excluded from conflict checking
            # and replaced by the new parse, so the operation is checked
            # against the corpus as it will look afterwards.
            parsed = core.parse_csv(
                conn, new_template, batch["content"], allow_empty=True
            )
            conflicts = core.find_conflicts(
                conn,
                parsed.rows,
                subject_label=f"批次 #{batch_id}「{batch['filename']}」",
                exclude_batch_id=batch_id,
            )
            if not conflicts:
                changes = core.switch_batch_template(
                    conn,
                    batch_id=batch_id,
                    new_template_id=new_template_id,
                    parsed=parsed,
                )
    except core.ParseError as exc:
        parse_errors = [
            f"无法按模板「{new_template['name']}」重新解释该文件：{issue}"
            for issue in exc.issues
        ]
    if parse_errors:
        batch = _load_batch_or_404(conn, batch_id)
        return (
            render_template(
                "batch_detail.html",
                batch=batch,
                declarations=_batch_declarations(conn, batch_id),
                changes=_batch_changes(conn, batch_id),
                notes=_batch_notes(batch),
                templates=_template_options(conn),
                errors=parse_errors,
            ),
            400,
        )
    if conflicts:
        return (
            render_template(
                "conflict.html",
                title="改选模板被拒绝：同修订号出现不同值",
                conflicts=conflicts,
                back_url=url_for("main.batch_detail", batch_id=batch_id),
                back_label=f"返回批次 #{batch_id}",
                detail=f"批次 #{batch_id} 的模板未改动，"
                "原档、申报与底表均保持原状。冲突覆盖本批的新申报与其它既有"
                "申报，即使该修订号不是最高修订号也会列出；请核对后重试。",
            ),
            409,
        )

    removed = sum(1 for c in changes if c["action"] == "removed")
    updated = sum(1 for c in changes if c["action"] == "set")
    flash(
        f"批次 #{batch_id} 已改选模板「{new_template['name']}」："
        f"底表更新 {updated} 项，撤下 {removed} 项。原件仍按原字节保留。",
        "success" if not removed else "warning",
    )
    return redirect(url_for("main.batch_detail", batch_id=batch_id))


# ---------------------------------------------------------------------------
# specimens
# ---------------------------------------------------------------------------


@bp.get("/specimens")
def specimens():
    conn = get_db()
    query = request.args.get("q", "").strip()
    rows = core.list_specimens(conn, query)
    if query and len(rows) == 1:
        return redirect(url_for("main.specimen_detail", specimen_id=rows[0]["id"]))
    return render_template("specimens.html", specimens=rows, query=query)


@bp.get("/specimens/<int:specimen_id>")
def specimen_detail(specimen_id: int):
    conn = get_db()
    specimen = conn.execute(
        "SELECT * FROM specimens WHERE id = ?", (specimen_id,)
    ).fetchone()
    if specimen is None:
        abort(404, description=f"试件 #{specimen_id} 不存在")
    current = core.specimen_current(conn, specimen_id)
    history = core.specimen_declaration_history(conn, specimen_id)
    current_by_indicator = {row["indicator_id"]: row for row in current}

    groups: dict[int, list[dict[str, Any]]] = {}
    for group in history:
        groups.setdefault(group["indicator_id"], []).append(group)

    indicators = conn.execute(
        """
        SELECT i.* FROM indicators i
        WHERE i.id IN (
            SELECT indicator_id FROM base_values WHERE specimen_id = ?
            UNION
            SELECT indicator_id FROM declarations WHERE specimen_id = ?
        )
        ORDER BY i.name, i.code
        """,
        (specimen_id, specimen_id),
    ).fetchall()

    blocks = [
        {
            "indicator": indicator,
            "current": current_by_indicator.get(indicator["id"]),
            "groups": groups.get(indicator["id"], []),
        }
        for indicator in indicators
    ]
    return render_template("specimen_detail.html", specimen=specimen, blocks=blocks)


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


@bp.app_errorhandler(404)
def not_found(error):
    return render_template("error.html", title="未找到", message=error.description), 404


@bp.app_errorhandler(413)
def too_large(error):
    return (
        render_template(
            "error.html", title="文件过大", message="上传文件超过 8 MB 限制。"
        ),
        413,
    )


@bp.app_errorhandler(400)
def bad_request(error):
    return (
        render_template("error.html", title="请求无效", message=error.description),
        400,
    )
