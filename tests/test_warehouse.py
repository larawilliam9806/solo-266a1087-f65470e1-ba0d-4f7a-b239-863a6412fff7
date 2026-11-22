import re
import sqlite3

import pytest

from warehouse import core


def upload(client, template_id, text, filename="batch.csv"):
    return client.post(
        "/upload",
        data={
            "template_id": str(template_id),
            "file": (open_bytes(text), filename),
        },
        content_type="multipart/form-data",
    )


def open_bytes(text):
    import io

    return io.BytesIO(text.encode("utf-8"))


def base(db, number, indicator_id):
    row = db.execute(
        """
        SELECT bv.*, s.number FROM base_values bv
        JOIN specimens s ON s.id = bv.specimen_id
        WHERE s.number = ? AND bv.indicator_id = ?
        """,
        (number, indicator_id),
    ).fetchone()
    return row


def test_upload_sets_base_values(client, meta, db):
    resp = upload(
        client,
        meta["measured_tpl"],
        "试件编号,修订号,强度,劈裂\nC-1,1,32.4,2.81\n",
    )
    assert resp.status_code == 302

    row = base(db, "C-1", meta["measured"])
    assert row["value_micro"] == 32400
    assert row["revision"] == 1
    assert base(db, "C-1", meta["split"])["value_micro"] == 2810


def test_patch_keeps_untouched_indicators(client, meta, db):
    upload(client, meta["measured_tpl"], "试件编号,修订号,强度,劈裂\nC-1,1,32.4,2.81\n")
    resp = upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-1,2,33.0\n")
    assert resp.status_code == 302

    strength = base(db, "C-1", meta["measured"])
    assert strength["value_micro"] == 33000
    assert strength["revision"] == 2
    # The patch did not carry 劈裂, so the old revision-1 value stays.
    split = base(db, "C-1", meta["split"])
    assert split["value_micro"] == 2810
    assert split["revision"] == 1


def test_highest_revision_wins_even_from_older_upload(client, meta, db):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-1,2,33.0\n")
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-1,1,32.4\n")
    row = base(db, "C-1", meta["measured"])
    assert row["value_micro"] == 33000
    assert row["revision"] == 2


def test_lower_revision_upload_is_recorded_but_not_base(client, meta, db):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-1,2,33.0\n")
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-1,1,32.4\n")
    rows = db.execute("SELECT revision, value_micro FROM declarations").fetchall()
    assert {(r["revision"], r["value_micro"]) for r in rows} == {
        (1, 32400),
        (2, 33000),
    }


def test_same_revision_same_value_from_two_batches_is_corroboration(client, meta, db):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-1,2,33.0\n", "a.csv")
    resp = upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-1,2,33.0\n", "b.csv")
    assert resp.status_code == 302
    batches = db.execute(
        "SELECT COUNT(DISTINCT batch_id) FROM declarations"
    ).fetchone()[0]
    assert batches == 2


def test_same_revision_different_value_rejected(client, meta, db):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-1,2,33.0\n", "a.csv")
    before = db.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    base_before = dict(base(db, "C-1", meta["measured"]))

    resp = upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-1,2,34.0\n", "b.csv")
    assert resp.status_code == 409
    assert "C-1" in resp.get_data(as_text=True)
    assert "33" in resp.get_data(as_text=True)
    assert "34" in resp.get_data(as_text=True)

    assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == before
    assert dict(base(db, "C-1", meta["measured"])) == base_before


def test_conflict_detected_within_one_file(client, meta, db):
    resp = upload(
        client,
        meta["patch_tpl"],
        "试件编号,修订号,强度\nC-1,2,33.0\nC-1,2,34.0\n",
    )
    assert resp.status_code == 409
    assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM declarations").fetchone()[0] == 0


def test_same_header_maps_to_different_indicator_per_template(client, meta, db):
    upload(
        client,
        meta["measured_tpl"],
        "试件编号,修订号,强度,劈裂\nC-9,1,30.0,3.0\n",
    )
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-9,1,27.5\n")

    assert base(db, "C-9", meta["measured"])["value_micro"] == 30000
    assert base(db, "C-9", meta["converted"])["value_micro"] == 27500
    assert base(db, "C-9", meta["split"])["value_micro"] == 3000


def test_template_switch_reinterprets_and_withdraws(client, meta, db):
    # Uploaded as the "converted" template by mistake.
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-3,1,28.7\n")
    assert base(db, "C-3", meta["converted"])["value_micro"] == 28700
    assert base(db, "C-3", meta["measured"]) is None

    resp = client.post(
        "/batches/1/switch",
        data={"template_id": str(meta["measured_tpl"])},
    )
    assert resp.status_code == 302

    # Now 强度 means measured; the converted indicator lost its only source.
    assert base(db, "C-3", meta["measured"])["value_micro"] == 28700
    assert base(db, "C-3", meta["converted"]) is None
    assert base(db, "C-3", meta["split"]) is None

    # Original bytes preserved.
    batch = db.execute("SELECT * FROM batches WHERE id = 1").fetchone()
    assert batch["content"].decode("utf-8") == "试件编号,修订号,强度\nC-3,1,28.7\n"
    assert batch["original_template_id"] == meta["converted_tpl"]
    assert batch["template_id"] == meta["measured_tpl"]


def test_template_switch_can_restore_and_remove(client, meta, db):
    # 实测模板 batch carries 强度 + 劈裂.
    upload(client, meta["measured_tpl"], "试件编号,修订号,强度,劈裂\nC-4,1,31.0,2.9\n")
    # Re-interpret with 补页模板, which only knows 强度: 劈裂 disappears.
    assert client.post(
        "/batches/1/switch", data={"template_id": str(meta["patch_tpl"])}
    ).status_code == 302
    assert base(db, "C-4", meta["split"]) is None
    assert base(db, "C-4", meta["measured"])["value_micro"] == 31000

    # Switch back: 劈裂 returns from the preserved original bytes.
    assert client.post(
        "/batches/1/switch", data={"template_id": str(meta["measured_tpl"])}
    ).status_code == 302
    assert base(db, "C-4", meta["split"])["value_micro"] == 2900

    changes = db.execute(
        "SELECT from_template_id, to_template_id FROM batch_template_changes"
    ).fetchall()
    assert len(changes) == 2


def test_template_switch_conflict_rejected_atomically(client, meta, db):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-5,1,30.0\n", "a.csv")
    # Second batch uploaded against 折算模板 gives a different value for the
    # same revision of the same specimen, but a different indicator, so no
    # conflict yet.
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-5,1,26.0\n", "b.csv")

    snapshot_decl = [
        dict(r)
        for r in db.execute(
            "SELECT * FROM declarations ORDER BY id"
        ).fetchall()
    ]
    snapshot_base = [
        dict(r) for r in db.execute("SELECT * FROM base_values ORDER BY specimen_id").fetchall()
    ]

    # Re-interpreting batch #2 with 补页模板 (measured strength) collides with
    # batch #1: same specimen/indicator/revision, different value.
    resp = client.post("/batches/2/switch", data={"template_id": str(meta["patch_tpl"])})
    assert resp.status_code == 409
    text = resp.get_data(as_text=True)
    assert "C-5" in text and "30" in text and "26" in text

    assert [dict(r) for r in db.execute("SELECT * FROM declarations ORDER BY id").fetchall()] == snapshot_decl
    assert [dict(r) for r in db.execute("SELECT * FROM base_values ORDER BY specimen_id").fetchall()] == snapshot_base
    batch = db.execute("SELECT * FROM batches WHERE id = 2").fetchone()
    assert batch["template_id"] == meta["converted_tpl"]


def test_switch_to_template_with_no_matching_columns_withdraws_everything(client, meta, db):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-6,1,30.0\n")
    # A template whose indicator columns are absent from the file.
    cur = db.execute(
        "INSERT INTO templates (name, specimen_header, revision_header, created_at)"
        " VALUES ('其它模板', '试件编号', '修订号', ?)",
        (core.now_iso(),),
    )
    other = cur.lastrowid
    db.execute(
        "INSERT INTO template_columns (template_id, header, indicator_id) VALUES (?, '别的列', ?)",
        (other, meta["split"]),
    )
    db.commit()

    resp = client.post("/batches/1/switch", data={"template_id": str(other)})
    assert resp.status_code == 302
    assert base(db, "C-6", meta["measured"]) is None
    assert db.execute("SELECT COUNT(*) FROM declarations").fetchone()[0] == 0


def test_specimen_page_lists_current_value_revision_and_all_same_revision_batches(
    client, meta, db
):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-7,1,30.0\n", "a.csv")
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-7,2,31.0\n", "b.csv")
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-7,2,31.0\n", "c.csv")

    specimen_id = db.execute(
        "SELECT id FROM specimens WHERE number = 'C-7'"
    ).fetchone()[0]
    html = client.get(f"/specimens/{specimen_id}").get_data(as_text=True)

    assert "31" in html
    assert "现值" in html
    assert "#2" in html and "#3" in html  # both same-revision batches shown
    assert "#1" in html  # older revision listed too
    assert "a.csv" in html and "b.csv" in html and "c.csv" in html


def test_batch_original_download_is_byte_identical(client, meta, db):
    raw = "试件编号,修订号,强度\nC-8,1,30.0\n"
    upload(client, meta["measured_tpl"], raw)
    resp = client.get("/batches/1/download")
    assert resp.status_code == 200
    assert resp.data == raw.encode("utf-8")


def test_revision_formats_and_fullwidth_values(client, meta, db):
    upload(
        client,
        meta["patch_tpl"],
        "试件编号,修订号,强度\nC-10,第2次,３２．５\n",
    )
    row = base(db, "C-10", meta["measured"])
    assert row["revision"] == 2
    assert row["revision_raw"] == "第2次"
    assert row["value_micro"] == 32500


def test_missing_required_column_rejected(client, meta, db):
    resp = upload(client, meta["patch_tpl"], "试件编号,强度\nC-1,30\n")
    assert resp.status_code == 400
    assert "修订号" in resp.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0


def test_blank_indicator_cell_is_not_a_declaration(client, meta, db):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-1,1,\n")
    assert db.execute("SELECT COUNT(*) FROM declarations").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1


def test_template_registration_via_web(client, meta, db):
    resp = client.post(
        "/templates",
        data={
            "name": "新模板",
            "specimen_header": "编号",
            "revision_header": "修订",
            "header": ["强度"],
            "indicator_id": [str(meta["converted"])],
        },
    )
    assert resp.status_code == 302
    tpl = db.execute("SELECT * FROM templates WHERE name = '新模板'").fetchone()
    assert tpl is not None
    column = db.execute(
        "SELECT * FROM template_columns WHERE template_id = ?", (tpl["id"],)
    ).fetchone()
    assert column["header"] == "强度"
    assert column["indicator_id"] == meta["converted"]


def test_rejected_switch_keeps_template_change_log_empty(client, meta, db):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-11,1,30.0\n", "a.csv")
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-11,1,26.0\n", "b.csv")
    resp = client.post("/batches/2/switch", data={"template_id": str(meta["patch_tpl"])})
    assert resp.status_code == 409
    assert db.execute("SELECT COUNT(*) FROM batch_template_changes").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM declarations").fetchone()[0] == 2


def test_upload_without_template_or_file_shows_errors(client, meta):
    resp = client.post("/upload", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    text = resp.get_data(as_text=True)
    assert "请选择报告模板" in text
    assert "请选择要上传的 CSV 文件" in text


def test_csrf_required_when_enabled(app, meta):
    app.config["CSRF_ENABLED"] = True
    client = app.test_client()
    resp = client.post(
        "/batches/1/switch", data={"template_id": "1"}, follow_redirects=False
    )
    assert resp.status_code == 400

    page = client.get("/upload")
    token = page.get_data(as_text=True).split('name="_csrf" value="')[1].split('"')[0]
    resp = client.post("/batches/1/switch", data={"template_id": "1", "_csrf": token})
    assert resp.status_code == 404  # no batch 1 exists; CSRF passed


def test_conflict_report_lists_every_source(client, meta, db):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-12,2,33.0\n", "first.csv")
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-12,2,33.0\n", "second.csv")
    resp = upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-12,2,34.0\n", "third.csv")
    assert resp.status_code == 409
    text = resp.get_data(as_text=True)
    assert "first.csv" in text and "second.csv" in text and "third.csv" in text
    assert "抗压强度（实测）" in text
    assert "第 2 行" in text


def test_specimen_page_shows_revision_and_template_change_origin(client, meta, db):
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-13,1,26.0\n")
    client.post("/batches/1/switch", data={"template_id": str(meta["patch_tpl"])})
    specimen_id = db.execute(
        "SELECT id FROM specimens WHERE number = 'C-13'"
    ).fetchone()["id"]
    text = client.get(f"/specimens/{specimen_id}").get_data(as_text=True)
    assert "折算模板" in text  # original template is shown for the batch
    assert "补页模板" in text  # current template is shown
    assert "采用修订号" in text


def test_switch_withdrawing_highest_revision_falls_back(client, meta, db):
    upload(client, meta["measured_tpl"], "试件编号,修订号,强度,劈裂\nC-14,2,33.0,2.9\n", "a.csv")
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-14,3,34.0\n", "b.csv")
    assert base(db, "C-14", meta["measured"])["revision"] == 3

    # Re-interpret batch #2 with a template that has no matching column, so it
    # stops declaring rev 3.  Base falls back to rev 2 from batch #1.
    cur = db.execute(
        "INSERT INTO templates (name, specimen_header, revision_header, created_at)"
        " VALUES ('无匹配模板', '试件编号', '修订号', ?)",
        (core.now_iso(),),
    )
    other = cur.lastrowid
    db.execute(
        "INSERT INTO template_columns (template_id, header, indicator_id) VALUES (?, '别列', ?)",
        (other, meta["split"]),
    )
    db.commit()

    assert client.post("/batches/2/switch", data={"template_id": str(other)}).status_code == 302
    row = base(db, "C-14", meta["measured"])
    assert row["revision"] == 2
    assert row["value_micro"] == 33000
    assert row["batch_id"] == 1
    # batch #2 no longer declares anything
    assert db.execute(
        "SELECT COUNT(*) FROM declarations WHERE batch_id = 2"
    ).fetchone()[0] == 0


def test_switch_moves_base_source_between_same_revision_batches(client, meta, db):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-15,1,30.0\n", "a.csv")
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-15,1,30.0\n", "b.csv")
    # Latest upload (batch #2) is the source of the tie.
    assert base(db, "C-15", meta["measured"])["batch_id"] == 2
    cur = db.execute(
        "INSERT INTO templates (name, specimen_header, revision_header, created_at)"
        " VALUES ('无匹配模板2', '试件编号', '修订号', ?)",
        (core.now_iso(),),
    )
    other = cur.lastrowid
    db.execute(
        "INSERT INTO template_columns (template_id, header, indicator_id) VALUES (?, '别列', ?)",
        (other, meta["split"]),
    )
    db.commit()
    assert client.post("/batches/2/switch", data={"template_id": str(other)}).status_code == 302
    # batch #1 still supplies the identical value.
    row = base(db, "C-15", meta["measured"])
    assert row["batch_id"] == 1
    assert row["value_micro"] == 30000


def test_csv_with_no_data_rows_still_archived(client, meta, db):
    resp = upload(client, meta["patch_tpl"], "试件编号,修订号,强度\n")
    assert resp.status_code == 302
    assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM declarations").fetchone()[0] == 0


def test_invalid_value_rejects_whole_upload(client, meta, db):
    text = "试件编号,修订号,强度\nC-16,1,30.0\nC-17,1,abc\n"
    resp = upload(client, meta["patch_tpl"], text)
    assert resp.status_code == 400
    assert "abc" in resp.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM specimens").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# template switch: batched conflict check / base recompute semantics
# ---------------------------------------------------------------------------


def _count_queries(conn, action):
    """Run ``action`` and return (result, number of SQL statements executed)."""
    executed: list[str] = []

    def trace(statement):
        executed.append(statement)

    conn.set_trace_callback(trace)
    try:
        result = action()
    finally:
        conn.set_trace_callback(None)
    return result, executed


def _add_template(db, name, header, indicator_id):
    cur = db.execute(
        "INSERT INTO templates (name, specimen_header, revision_header, created_at)"
        " VALUES (?, '试件编号', '修订号', ?)",
        (name, core.now_iso()),
    )
    template_id = cur.lastrowid
    db.execute(
        "INSERT INTO template_columns (template_id, header, indicator_id)"
        " VALUES (?, ?, ?)",
        (template_id, header, indicator_id),
    )
    db.commit()
    return template_id


def test_switch_conflict_on_lower_revision_is_rejected(client, meta, db):
    # Batch #1 declares revision 1 = 30.0 (highest for C-20).
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-20,1,30.0\n", "a.csv")
    # Batch #2 is a converted-template file whose 强度 currently maps to the
    # converted indicator, revision 1 = 26.0.
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-20,1,26.0\n", "b.csv")
    # A revision-2 declaration from yet another batch makes revision 1 not the
    # highest revision; the conflict must still be detected.
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-20,2,35.0\n", "c.csv")

    before_batch = dict(db.execute("SELECT * FROM batches WHERE id = 2").fetchone())
    before_decls = [
        dict(r) for r in db.execute("SELECT * FROM declarations ORDER BY id").fetchall()
    ]
    before_base = [
        dict(r) for r in db.execute("SELECT * FROM base_values ORDER BY specimen_id").fetchall()
    ]

    resp = client.post("/batches/2/switch", data={"template_id": str(meta["patch_tpl"])})
    assert resp.status_code == 409
    text = resp.get_data(as_text=True)
    assert "C-20" in text
    assert "30" in text and "26" in text
    assert "a.csv" in text and "b.csv" in text  # both sources listed

    assert dict(db.execute("SELECT * FROM batches WHERE id = 2").fetchone()) == before_batch
    assert [
        dict(r) for r in db.execute("SELECT * FROM declarations ORDER BY id").fetchall()
    ] == before_decls
    assert [
        dict(r) for r in db.execute("SELECT * FROM base_values ORDER BY specimen_id").fetchall()
    ] == before_base


def test_switch_replaces_old_declarations_before_conflict_check(client, meta, db):
    # The batch's own old declarations must not conflict with its new ones.
    # Batch #1: 折算模板 revision 1 = 26.0 for C-21.  Batch #2 supplies 30.0
    # for the measured indicator, revision 1.
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-21,1,26.0\n", "a.csv")
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-21,1,30.0\n", "b.csv")

    # Switching batch #1 to the measured template yields 26.0 for the measured
    # indicator at revision 1, which collides with batch #2 -> rejected.
    resp = client.post("/batches/1/switch", data={"template_id": str(meta["patch_tpl"])})
    assert resp.status_code == 409
    assert "26" in resp.get_data(as_text=True)
    assert "30" in resp.get_data(as_text=True)

    # Switching the same batch to a template that maps 强度 to the measured
    # indicator must collide too, and its own old (converted) declarations are
    # replaced rather than compared against themselves.
    resp = client.post("/batches/1/switch", data={"template_id": str(meta["measured_tpl"])})
    assert resp.status_code == 409
    assert db.execute(
        "SELECT COUNT(*) FROM declarations WHERE batch_id = 1 AND indicator_id = ?",
        (meta["converted"],),
    ).fetchone()[0] == 1


def test_switch_recomputes_only_affected_specimens_and_indicators(client, meta, db):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-30,1,30.0\n", "a.csv")
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-31,1,31.0\n", "b.csv")
    # An unrelated specimen with a split-tensile value only.
    upload(client, meta["measured_tpl"], "试件编号,修订号,劈裂\nC-32,1,2.5\n", "c.csv")

    untouched_before = dict(
        db.execute(
            """
            SELECT bv.* FROM base_values bv JOIN specimens s ON s.id = bv.specimen_id
            WHERE s.number = 'C-31' AND bv.indicator_id = ?
            """,
            (meta["measured"],),
        ).fetchone()
    )
    split_before = dict(
        db.execute(
            """
            SELECT bv.* FROM base_values bv JOIN specimens s ON s.id = bv.specimen_id
            WHERE s.number = 'C-32' AND bv.indicator_id = ?
            """,
            (meta["split"],),
        ).fetchone()
    )

    # Switch batch #1 to a template that no longer matches any column, so it
    # stops declaring measured strength for C-30 only.
    other = _add_template(db, "无匹配", "别列", meta["split"])
    resp = client.post("/batches/1/switch", data={"template_id": str(other)})
    assert resp.status_code == 302

    assert base(db, "C-30", meta["measured"]) is None
    # C-31 and C-32 rows are untouched -- byte-for-byte, including timestamps.
    assert dict(
        db.execute(
            """
            SELECT bv.* FROM base_values bv JOIN specimens s ON s.id = bv.specimen_id
            WHERE s.number = 'C-31' AND bv.indicator_id = ?
            """,
            (meta["measured"],),
        ).fetchone()
    ) == untouched_before
    assert dict(
        db.execute(
            """
            SELECT bv.* FROM base_values bv JOIN specimens s ON s.id = bv.specimen_id
            WHERE s.number = 'C-32' AND bv.indicator_id = ?
            """,
            (meta["split"],),
        ).fetchone()
    ) == split_before


def test_switch_updates_base_when_only_source_batch_changes(client, meta, db):
    # Two batches corroborate revision 1 = 30.0 for C-33; the later upload is
    # the base source.
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-33,1,30.0\n", "a.csv")
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-33,1,30.0\n", "b.csv")
    assert base(db, "C-33", meta["measured"])["batch_id"] == 2

    before_updated_at = base(db, "C-33", meta["measured"])["updated_at"]

    # Switch batch #2 away (no matching columns): batch #1 still supplies the
    # same value and revision, but the source batch changes.
    other = _add_template(db, "无匹配2", "别列", meta["split"])
    assert client.post("/batches/2/switch", data={"template_id": str(other)}).status_code == 302

    row = base(db, "C-33", meta["measured"])
    assert row["batch_id"] == 1
    assert row["value_micro"] == 30000
    assert row["revision"] == 1
    assert row["updated_at"] > before_updated_at


def test_switch_does_not_rewrite_unchanged_base_rows(client, meta, db):
    # A batch carrying 强度 + 劈裂, and a later patch corroborating 强度 at the
    # same revision, so the patch batch is the base source for 强度.
    upload(client, meta["measured_tpl"], "试件编号,修订号,强度,劈裂\nC-34,1,30.0,2.5\n", "a.csv")
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-34,1,30.0\n", "b.csv")
    # An independent specimen, untouched by the switch below.
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-35,1,40.0\n", "c.csv")

    before = {
        (r["specimen_id"], r["indicator_id"]): dict(r)
        for r in db.execute("SELECT * FROM base_values").fetchall()
    }
    # Both templates map 强度 to the measured indicator, so switching batch #2
    # re-parses to the exact same declarations.  Both of C-34's pairs are
    # recomputed, but neither value, revision nor source changes, so no row may
    # be rewritten (timestamps included).
    assert client.post(
        "/batches/2/switch", data={"template_id": str(meta["patch_tpl"])}
    ).status_code == 302

    after = {
        (r["specimen_id"], r["indicator_id"]): dict(r)
        for r in db.execute("SELECT * FROM base_values").fetchall()
    }
    assert set(after) == set(before)
    for key, row in after.items():
        assert row == before[key], f"row {key} was rewritten without a content change"


def test_switch_scales_batchwise_without_per_key_queries(client, meta, db):
    # 40 specimens x 2 indicators, so any per-key lookup would show up.
    rows = "".join(
        f"C-{100 + i},1,{30 + i * 0.1:.1f},2.5\n" for i in range(40)
    )
    upload(client, meta["measured_tpl"], "试件编号,修订号,强度,劈裂\n" + rows)

    _, statements = _count_queries(
        db,
        lambda: client.post(
            "/batches/1/switch", data={"template_id": str(meta["patch_tpl"])}
        ),
    )
    # 80 specimen/indicator pairs are recomputed; the statement count must stay
    # flat instead of growing with each pair.  The fixed overhead is roughly a
    # dozen conflict-check statements plus a dozen recompute statements.
    assert len(statements) < 50, f"{len(statements)} statements: {statements}"
    # And nothing may issue one statement per pair.
    assert len(statements) < len(rows.splitlines()) * 2


def test_switch_does_not_scan_unrelated_declarations(client, meta, db):
    # A lot of noise for other specimens.
    noise = "".join(f"D-{i},1,{20 + i * 0.5:.1f}\n" for i in range(300))
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\n" + noise, "noise.csv")
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-40,1,28.0\n", "target.csv")
    db.commit()

    statements = _capture_switch_queries(client, db, 2, meta["patch_tpl"])
    unconstrained = [
        q
        for q in statements
        if "FROM declarations" in q and "temp._" not in q and "WHERE" not in q
    ]
    assert not unconstrained, unconstrained

    # And every declaration / base-table read must use an index rather than a
    # full scan, so unrelated rows are never visited.
    _replay_staged_keys(db, statements)
    scanned = []
    for q in statements:
        if "FROM declarations" not in q and "FROM base_values" not in q:
            continue
        plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + q)]
        # Scans of the operation's own co-routines / temp keys are fine; a scan
        # of the real declaration or base table is not.
        table_scans = [
            line
            for line in plan
            if re.match(
                r"SCAN (?:[a-zA-Z_]+|SQLITE_)", line
            )
            and "SUBQUERY" not in line.upper()
        ]
        if table_scans:
            scanned.append((q, plan))
    assert not scanned, scanned


def _replay_staged_keys(db, statements):
    """Recreate the operation's temp tables so captured SQL can be EXPLAINed."""
    for statement in statements:
        head = statement.lstrip().upper()
        if head.startswith(("CREATE TEMP TABLE", "INSERT INTO TEMP.")):
            db.execute(statement)


def _capture_switch_queries(client, db, batch_id, template_id):
    statements: list[str] = []

    def capturing_trace(statement):
        statements.append(statement)

    db.set_trace_callback(capturing_trace)
    try:
        resp = client.post(
            f"/batches/{batch_id}/switch", data={"template_id": str(template_id)}
        )
    finally:
        db.set_trace_callback(None)
    assert resp.status_code == 302
    return statements


def test_switch_write_failure_rolls_everything_back(client, meta, db, monkeypatch):
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-50,1,28.0\n", "a.csv")
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-50,1,28.0\n", "b.csv")
    db.commit()

    before = {
        "batch": dict(db.execute("SELECT * FROM batches WHERE id = 2").fetchone()),
        "declarations": [
            dict(r) for r in db.execute("SELECT * FROM declarations ORDER BY id").fetchall()
        ],
        "base": [
            dict(r) for r in db.execute("SELECT * FROM base_values ORDER BY specimen_id").fetchall()
        ],
        "changes": db.execute("SELECT COUNT(*) FROM batch_template_changes").fetchone()[0],
    }

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(core, "recompute_base", boom)
    with pytest.raises(sqlite3.OperationalError):
        client.post(
            "/batches/2/switch",
            data={"template_id": str(meta["measured_tpl"])},
        )

    assert dict(db.execute("SELECT * FROM batches WHERE id = 2").fetchone()) == before["batch"]
    assert [
        dict(r) for r in db.execute("SELECT * FROM declarations ORDER BY id").fetchall()
    ] == before["declarations"]
    assert [
        dict(r) for r in db.execute("SELECT * FROM base_values ORDER BY specimen_id").fetchall()
    ] == before["base"]
    assert (
        db.execute("SELECT COUNT(*) FROM batch_template_changes").fetchone()[0]
        == before["changes"]
    )
