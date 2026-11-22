"""Domain logic for the concrete specimen declaration warehouse.

The warehouse keeps three layers:

* ``batches`` -- every uploaded CSV, byte-for-byte, plus the template that is
  currently used to interpret it (``template_id``) and the template it was
  first uploaded with (``original_template_id``).
* ``declarations`` -- the (specimen, indicator, revision, value) tuples that
  result from interpreting a batch with its *current* template.
* ``base_values`` -- the current value per (specimen, indicator), picked from
  the declaration with the highest revision number.

A batch is always interpreted as a whole.  Validation and conflict detection
happen before any write, so a rejected upload / template switch leaves both
the archive and the base table untouched.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Sequence

import sqlite3

MAX_VALUE_MICRO = 1_000_000_000  # 1000.000 MPa
MAX_SPECIMEN_LENGTH = 128


class ParseError(Exception):
    """The CSV cannot be interpreted with the selected template."""

    def __init__(self, issues: Sequence[str]):
        self.issues = list(issues)
        super().__init__("; ".join(self.issues))


class ValueError_(ValueError):
    """Bad cell value / revision."""


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def format_micro(micro: int) -> str:
    """12500 -> '12.5', 12345 -> '12.345'."""
    sign = "-" if micro < 0 else ""
    micro = abs(micro)
    whole, frac = divmod(micro, 1000)
    if frac == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{frac:03d}".rstrip("0")


def parse_value(raw: str) -> int:
    """Parse a strength value in MPa into integer micro-units.

    Full-width digits are normalised.  More than three decimals are rounded
    half-up to the stored precision (the caller reports it as a warning).
    """
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    if not text:
        raise ValueError_("值为空")
    if not re.fullmatch(r"\d+(?:\.\d+)?|\.\d+", text):
        raise ValueError_(f"「{raw}」不是有效的强度值（MPa）")
    try:
        dec = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - regex already guards
        raise ValueError_(f"「{raw}」不是有效的强度值（MPa）") from exc
    quantized = dec.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    micro = int(quantized * 1000)
    if micro > MAX_VALUE_MICRO:
        raise ValueError_(f"「{raw}」超出允许范围（0–1000 MPa）")
    return micro


_VALUE_DECIMALS = re.compile(r"\.(\d+)")


def value_has_extra_precision(raw: str) -> bool:
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    match = _VALUE_DECIMALS.search(text)
    return bool(match and len(match.group(1)) > 3)


_REVISION_RE = re.compile(
    r"^(?:第\s*)?(\d+)\s*(?:次|版|rev(?:ision)?\.?)?$", re.IGNORECASE
)


def parse_revision(raw: str) -> tuple[int, str]:
    """Return (revision number, raw text to show in the UI)."""
    original = str(raw).strip()
    text = unicodedata.normalize("NFKC", original)
    match = _REVISION_RE.fullmatch(text)
    if not match:
        raise ValueError_(f"「{raw}」不是有效的报告修订号（应如 1、第2次、Rev3）")
    revision = int(match.group(1))
    return revision, original


def normalize_specimen(raw: str) -> str:
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    if not text:
        raise ValueError_("试件号为空")
    if len(text) > MAX_SPECIMEN_LENGTH:
        raise ValueError_(f"试件号过长（最多 {MAX_SPECIMEN_LENGTH} 个字符）")
    if any(ord(ch) < 32 for ch in text):
        raise ValueError_("试件号包含不可见控制字符")
    return text


# ---------------------------------------------------------------------------
# template loading
# ---------------------------------------------------------------------------


def load_template(conn: sqlite3.Connection, template_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM templates WHERE id = ?", (template_id,)
    ).fetchone()


def template_mapping(conn: sqlite3.Connection, template_id: int) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT tc.header, i.id AS indicator_id, i.code, i.name, i.unit
        FROM template_columns tc
        JOIN indicators i ON i.id = tc.indicator_id
        WHERE tc.template_id = ?
        """,
        (template_id,),
    ).fetchall()
    return {
        row["header"]: {
            "indicator_id": row["indicator_id"],
            "code": row["code"],
            "name": row["name"],
            "unit": row["unit"],
        }
        for row in rows
    }


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------


@dataclass
class ParsedRow:
    specimen_number: str
    indicator_id: int
    indicator_code: str
    indicator_name: str
    revision: int
    revision_raw: str
    value_micro: int
    line: int
    header: str


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ignored_headers: list[str] = field(default_factory=list)
    absent_indicators: list[dict[str, str]] = field(default_factory=list)
    present_indicators: list[dict[str, str]] = field(default_factory=list)
    data_rows: int = 0

    def notes(self) -> dict[str, Any]:
        return {
            "warnings": self.warnings,
            "ignored_headers": self.ignored_headers,
            "absent_indicators": self.absent_indicators,
            "present_indicators": self.present_indicators,
            "data_rows": self.data_rows,
        }


def decode_csv(content: bytes) -> str:
    if not content:
        raise ParseError(["文件为空"])
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParseError(["无法识别文件编码（请使用 UTF-8 或 GB18030 编码的 CSV）"])


def parse_csv(
    conn: sqlite3.Connection,
    template: sqlite3.Row,
    content: bytes,
    *,
    allow_empty: bool = False,
) -> ParseResult:
    """Interpret a CSV with a template.

    With ``allow_empty=True`` (used when re-selecting a template for an
    already archived batch) a file whose columns do not match any indicator of
    the template is accepted as declaring nothing; this is how a wrongly
    templated batch stops contributing values.  A new upload uses the strict
    mode.
    """
    mapping = template_mapping(conn, template["id"])
    if not mapping:
        raise ParseError([f"模板「{template['name']}」尚未登记任何指标列"])

    text = decode_csv(content)
    reader = csv.reader(io.StringIO(text, newline=""))
    result = ParseResult()

    try:
        header_row = next(reader)
    except StopIteration:
        raise ParseError(["CSV 没有表头行"]) from None

    headers = [h.strip() for h in header_row]
    headers = [unicodedata.normalize("NFKC", h) for h in headers]

    dupes = {h for h in headers if h and headers.count(h) > 1}
    if dupes:
        raise ParseError([f"表头重复：{', '.join(sorted(dupes))}"])

    specimen_header = template["specimen_header"]
    revision_header = template["revision_header"]
    mapped_headers = [h for h in headers if h in mapping]

    if not mapped_headers:
        if not allow_empty:
            raise ParseError(
                [f"CSV 没有任何列与模板「{template['name']}」登记的指标列名对应"]
            )
        ignored = [
            h for h in headers if h and h not in (specimen_header, revision_header)
        ]
        result.ignored_headers = ignored
        result.absent_indicators = [
            {"code": info["code"], "name": info["name"]}
            for info in mapping.values()
        ]
        result.warnings.append(
            f"文件列名与模板「{template['name']}」登记的指标列都不对应，"
            "该批次不再申报任何指标。"
        )
        return result

    issues: list[str] = []
    if specimen_header not in headers:
        issues.append(f"缺少试件号列「{specimen_header}」")
    if revision_header not in headers:
        issues.append(f"缺少报告修订号列「{revision_header}」")
    if issues:
        raise ParseError(issues)
    for header, info in mapping.items():
        if header not in headers:
            result.warnings.append(f"模板指标列「{header}」未出现在 CSV 中（不影响本次申报）")

    ignored = [
        h for h in headers if h and h not in mapping and h not in (specimen_header, revision_header)
    ]
    if ignored:
        result.ignored_headers = ignored
        result.warnings.append(
            "以下列未在模板中登记，已忽略：" + "、".join(ignored)
        )

    specimen_idx = headers.index(specimen_header)
    revision_idx = headers.index(revision_header)
    indicator_columns = [
        (idx, mapping[header]) for idx, header in enumerate(headers) if header in mapping
    ]

    result.present_indicators = [
        {"code": info["code"], "name": info["name"]}
        for header, info in mapping.items()
        if header in headers
    ]
    result.absent_indicators = [
        {"code": info["code"], "name": info["name"]}
        for header, info in mapping.items()
        if header not in headers
    ]

    for raw_row in reader:
        line = reader.line_num
        if not any(cell.strip() for cell in raw_row):
            continue
        if len(raw_row) > len(headers) and any(
            cell.strip() for cell in raw_row[len(headers):]
        ):
            issues.append(f"第 {line} 行：列数多于表头")
            continue
        cells = list(raw_row) + [""] * (len(headers) - len(raw_row))
        got_value = False
        try:
            specimen_number = normalize_specimen(cells[specimen_idx])
            revision, revision_raw = parse_revision(cells[revision_idx])
        except ValueError_ as exc:
            issues.append(f"第 {line} 行：{exc}")
            continue
        for idx, info in indicator_columns:
            raw_value = cells[idx].strip()
            if not raw_value:
                continue
            try:
                micro = parse_value(raw_value)
            except ValueError_ as exc:
                issues.append(f"第 {line} 行（{info['name']}）：{exc}")
                continue
            got_value = True
            if value_has_extra_precision(raw_value):
                result.warnings.append(
                    f"第 {line} 行（{info['name']}）值 {raw_value} 已四舍五入到 0.001 MPa"
                )
            result.rows.append(
                ParsedRow(
                    specimen_number=specimen_number,
                    indicator_id=info["indicator_id"],
                    indicator_code=info["code"],
                    indicator_name=info["name"],
                    revision=revision,
                    revision_raw=revision_raw,
                    value_micro=micro,
                    line=line,
                    header=info["name"],
                )
            )
        if not got_value:
            result.warnings.append(f"第 {line} 行：本次未申报任何指标值，已跳过")
        else:
            result.data_rows += 1

    if issues:
        raise ParseError(issues)
    return result


# ---------------------------------------------------------------------------
# conflict detection
# ---------------------------------------------------------------------------


def _conflict_key(row: ParsedRow) -> tuple[str, int, int]:
    return (row.specimen_number, row.indicator_id, row.revision)


def find_conflicts(
    conn: sqlite3.Connection,
    rows: Sequence[ParsedRow],
    *,
    subject_label: str,
    exclude_batch_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return conflicts without writing anything.

    A conflict exists when the same (specimen, indicator, revision) key would
    hold two different values after the operation.  Identical values from
    several batches are allowed (they are merely corroborating declarations).
    """
    grouped: dict[tuple[str, int, int], list[ParsedRow]] = {}
    for row in rows:
        grouped.setdefault(_conflict_key(row), []).append(row)

    specimen_ids = {}
    numbers = sorted({key[0] for key in grouped})
    if numbers:
        placeholders = ",".join("?" for _ in numbers)
        for fetched in conn.execute(
            f"SELECT id, number FROM specimens WHERE number IN ({placeholders})",
            numbers,
        ):
            specimen_ids[fetched["number"]] = fetched["id"]

    conflicts: list[dict[str, Any]] = []
    for (number, indicator_id, revision), group in grouped.items():
        indicator = conn.execute(
            "SELECT code, name, unit FROM indicators WHERE id = ?", (indicator_id,)
        ).fetchone()
        indicator_label = f"{indicator['name']}（{indicator['code']}）"

        entries: dict[int, list[str]] = {}
        for row in group:
            entries.setdefault(row.value_micro, []).append(
                f"{subject_label} 第 {row.line} 行"
            )

        specimen_id = specimen_ids.get(number)
        if specimen_id is not None:
            params: list[Any] = [specimen_id, indicator_id, revision]
            extra = ""
            if exclude_batch_id is not None:
                extra = " AND d.batch_id != ?"
                params.append(exclude_batch_id)
            for existing in conn.execute(
                f"""
                SELECT d.value_micro, d.row_number, b.id AS batch_id, b.filename
                FROM declarations d
                JOIN batches b ON b.id = d.batch_id
                WHERE d.specimen_id = ? AND d.indicator_id = ? AND d.revision = ?{extra}
                """,
                params,
            ):
                label = f"批次 #{existing['batch_id']}「{existing['filename']}」"
                if existing["row_number"]:
                    label += f" 第 {existing['row_number']} 行"
                entries.setdefault(existing["value_micro"], []).append(label)

        if len(entries) > 1:
            conflicts.append(
                {
                    "specimen": number,
                    "indicator": indicator_label,
                    "indicator_code": indicator["code"],
                    "revision": group[0].revision_raw
                    if group[0].revision_raw.strip()
                    else str(revision),
                    "revision_number": revision,
                    "options": [
                        {
                            "value": format_micro(value),
                            "sources": sources,
                        }
                        for value, sources in sorted(entries.items())
                    ],
                }
            )

    conflicts.sort(key=lambda c: (c["specimen"], c["indicator_code"], c["revision_number"]))
    return conflicts


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


def ensure_specimen(conn: sqlite3.Connection, number: str) -> int:
    row = conn.execute(
        "SELECT id FROM specimens WHERE number = ?", (number,)
    ).fetchone()
    if row:
        return row["id"]
    cursor = conn.execute(
        "INSERT INTO specimens (number, created_at) VALUES (?, ?)",
        (number, now_iso()),
    )
    return int(cursor.lastrowid)


def insert_declarations(
    conn: sqlite3.Connection, batch_id: int, rows: Iterable[ParsedRow]
) -> set[tuple[int, int]]:
    """Insert rows, de-duplicating identical keys inside the batch.

    Returns the set of (specimen_id, indicator_id) pairs the batch declares.
    """
    pairs: set[tuple[int, int]] = set()
    seen: dict[tuple[int, int, int], int] = {}
    for row in rows:
        specimen_id = ensure_specimen(conn, row.specimen_number)
        key = (specimen_id, row.indicator_id, row.revision)
        pairs.add((specimen_id, row.indicator_id))
        if key in seen:
            # Identical value: already validated as non-conflicting.
            continue
        conn.execute(
            """
            INSERT INTO declarations
                (batch_id, specimen_id, indicator_id, revision, revision_raw,
                 value_micro, row_number)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                specimen_id,
                row.indicator_id,
                row.revision,
                row.revision_raw,
                row.value_micro,
                row.line,
            ),
        )
        seen[key] = row.value_micro
    return pairs


def batch_pairs(conn: sqlite3.Connection, batch_id: int) -> set[tuple[int, int]]:
    return {
        (row["specimen_id"], row["indicator_id"])
        for row in conn.execute(
            "SELECT DISTINCT specimen_id, indicator_id FROM declarations WHERE batch_id = ?",
            (batch_id,),
        )
    }


def recompute_base(
    conn: sqlite3.Connection, pairs: Iterable[tuple[int, int]]
) -> list[dict[str, Any]]:
    """Recompute base rows for the given (specimen, indicator) pairs.

    Highest revision wins.  A tie on the revision number is resolved by the
    latest upload (values are guaranteed to agree by conflict detection).
    Pairs with no remaining declaration are removed from the base table.
    """
    changes: list[dict[str, Any]] = []
    for specimen_id, indicator_id in sorted(set(pairs)):
        old = conn.execute(
            "SELECT * FROM base_values WHERE specimen_id = ? AND indicator_id = ?",
            (specimen_id, indicator_id),
        ).fetchone()
        winner = conn.execute(
            """
            SELECT d.*, b.uploaded_at
            FROM declarations d
            JOIN batches b ON b.id = d.batch_id
            WHERE d.specimen_id = ? AND d.indicator_id = ?
            ORDER BY d.revision DESC, b.uploaded_at DESC, b.id DESC, d.id DESC
            LIMIT 1
            """,
            (specimen_id, indicator_id),
        ).fetchone()
        specimen = conn.execute(
            "SELECT number FROM specimens WHERE id = ?", (specimen_id,)
        ).fetchone()
        indicator = conn.execute(
            "SELECT code, name FROM indicators WHERE id = ?", (indicator_id,)
        ).fetchone()

        if winner is None:
            if old is not None:
                conn.execute(
                    "DELETE FROM base_values WHERE specimen_id = ? AND indicator_id = ?",
                    (specimen_id, indicator_id),
                )
                changes.append(
                    {
                        "action": "removed",
                        "specimen": specimen["number"],
                        "indicator": f"{indicator['name']}（{indicator['code']}）",
                        "old": format_micro(old["value_micro"]),
                        "new": None,
                        "revision": old["revision_raw"],
                    }
                )
            continue

        conn.execute(
            """
            INSERT INTO base_values
                (specimen_id, indicator_id, revision, revision_raw, value_micro,
                 batch_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (specimen_id, indicator_id) DO UPDATE SET
                revision = excluded.revision,
                revision_raw = excluded.revision_raw,
                value_micro = excluded.value_micro,
                batch_id = excluded.batch_id,
                updated_at = excluded.updated_at
            """,
            (
                specimen_id,
                indicator_id,
                winner["revision"],
                winner["revision_raw"],
                winner["value_micro"],
                winner["batch_id"],
                now_iso(),
            ),
        )
        if old is None or (
            old["value_micro"] != winner["value_micro"]
            or old["revision"] != winner["revision"]
            or old["batch_id"] != winner["batch_id"]
        ):
            changes.append(
                {
                    "action": "set",
                    "specimen": specimen["number"],
                    "indicator": f"{indicator['name']}（{indicator['code']}）",
                    "old": format_micro(old["value_micro"]) if old else None,
                    "new": format_micro(winner["value_micro"]),
                    "revision": winner["revision_raw"],
                }
            )
    return changes


# ---------------------------------------------------------------------------
# batch operations
# ---------------------------------------------------------------------------


def create_batch(
    conn: sqlite3.Connection,
    *,
    filename: str,
    content: bytes,
    template_id: int,
    parsed: ParseResult,
) -> int:
    """Insert a batch and its declarations, then refresh the base table.

    The caller must have run :func:`parse_csv` and :func:`find_conflicts`
    first.  Any failure rolls the caller's transaction back.
    """
    timestamp = now_iso()
    cursor = conn.execute(
        """
        INSERT INTO batches
            (filename, content, content_sha256, size_bytes, uploaded_at,
             template_id, original_template_id, parse_notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            filename,
            content,
            hashlib.sha256(content).hexdigest(),
            len(content),
            timestamp,
            template_id,
            template_id,
            json.dumps(parsed.notes(), ensure_ascii=False),
        ),
    )
    batch_id = int(cursor.lastrowid)
    pairs = insert_declarations(conn, batch_id, parsed.rows)
    recompute_base(conn, pairs)
    return batch_id


def switch_batch_template(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    new_template_id: int,
    parsed: ParseResult,
) -> list[dict[str, Any]]:
    """Re-interpret a stored batch with another template.

    The original bytes are never touched.  The caller must have validated the
    new parse and checked conflicts (excluding this batch) beforehand.
    """
    old_pairs = batch_pairs(conn, batch_id)
    old_template_id = conn.execute(
        "SELECT template_id FROM batches WHERE id = ?", (batch_id,)
    ).fetchone()["template_id"]
    timestamp = now_iso()
    conn.execute(
        "DELETE FROM declarations WHERE batch_id = ?", (batch_id,)
    )
    conn.execute(
        """
        UPDATE batches
        SET template_id = ?, template_changed_at = ?, parse_notes = ?
        WHERE id = ?
        """,
        (
            new_template_id,
            timestamp,
            json.dumps(parsed.notes(), ensure_ascii=False),
            batch_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO batch_template_changes
            (batch_id, from_template_id, to_template_id, changed_at)
        VALUES (?, ?, ?, ?)
        """,
        (batch_id, old_template_id, new_template_id, timestamp),
    )
    new_pairs = insert_declarations(conn, batch_id, parsed.rows)
    return recompute_base(conn, old_pairs | new_pairs)


# ---------------------------------------------------------------------------
# read models
# ---------------------------------------------------------------------------


def specimen_current(conn: sqlite3.Connection, specimen_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT bv.*, i.code, i.name, i.unit, b.filename, b.id AS source_batch_id,
               b.template_id AS source_template_id, t.name AS source_template_name
        FROM base_values bv
        JOIN indicators i ON i.id = bv.indicator_id
        JOIN batches b ON b.id = bv.batch_id
        LEFT JOIN templates t ON t.id = b.template_id
        WHERE bv.specimen_id = ?
        ORDER BY i.name, i.code
        """,
        (specimen_id,),
    ).fetchall()


def specimen_declaration_history(
    conn: sqlite3.Connection, specimen_id: int
) -> list[dict[str, Any]]:
    """Group all declarations by (indicator, revision).

    Each group lists every batch that contributed the value, satisfying
    "all same-revision batches that supplied the value".
    """
    rows = conn.execute(
        """
        SELECT d.indicator_id, i.code, i.name, i.unit, d.revision, d.revision_raw,
               d.value_micro, d.row_number, d.batch_id, b.filename, b.uploaded_at,
               b.original_template_id, ot.name AS original_template_name,
               b.template_id, ct.name AS current_template_name
        FROM declarations d
        JOIN indicators i ON i.id = d.indicator_id
        JOIN batches b ON b.id = d.batch_id
        LEFT JOIN templates ot ON ot.id = b.original_template_id
        LEFT JOIN templates ct ON ct.id = b.template_id
        WHERE d.specimen_id = ?
        ORDER BY i.name, i.code, d.revision DESC, b.uploaded_at, b.id
        """,
        (specimen_id,),
    ).fetchall()

    grouped: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        key = (row["indicator_id"], row["revision"])
        group = grouped.get(key)
        if group is None:
            group = {
                "indicator_id": row["indicator_id"],
                "code": row["code"],
                "name": row["name"],
                "unit": row["unit"],
                "revision": row["revision"],
                "revision_raw": row["revision_raw"],
                "value": format_micro(row["value_micro"]),
                "value_micro": row["value_micro"],
                "batches": [],
                "row_numbers": [],
            }
            grouped[key] = group
        group["batches"].append(
            {
                "id": row["batch_id"],
                "filename": row["filename"],
                "uploaded_at": row["uploaded_at"],
                "original_template_name": row["original_template_name"],
                "current_template_name": row["current_template_name"],
                "row_number": row["row_number"],
            }
        )
        if row["row_number"] is not None:
            group["row_numbers"].append(row["row_number"])

    return sorted(
        grouped.values(),
        key=lambda g: (g["name"], g["code"], -g["revision"]),
    )


def list_specimens(conn: sqlite3.Connection, query: str = "") -> list[sqlite3.Row]:
    if query.strip():
        pattern = f"%{query.strip()}%"
        return conn.execute(
            """
            SELECT s.*, COUNT(bv.indicator_id) AS indicator_count
            FROM specimens s
            LEFT JOIN base_values bv ON bv.specimen_id = s.id
            WHERE s.number LIKE ?
            GROUP BY s.id
            ORDER BY s.number
            LIMIT 200
            """,
            (pattern,),
        ).fetchall()
    return conn.execute(
        """
        SELECT s.*, COUNT(bv.indicator_id) AS indicator_count
        FROM specimens s
        LEFT JOIN base_values bv ON bv.specimen_id = s.id
        GROUP BY s.id
        ORDER BY s.number
        LIMIT 200
        """
    ).fetchall()


def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT b.*, ct.name AS current_template_name,
               ot.name AS original_template_name,
               (SELECT COUNT(*) FROM declarations d WHERE d.batch_id = b.id)
                   AS declaration_count
        FROM batches b
        LEFT JOIN templates ct ON ct.id = b.template_id
        LEFT JOIN templates ot ON ot.id = b.original_template_id
        ORDER BY b.uploaded_at DESC, b.id DESC
        """
    ).fetchall()
