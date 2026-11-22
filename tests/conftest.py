import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from warehouse import create_app  # noqa: E402
from warehouse.db import get_db  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    database = str(tmp_path / "test.db")
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": database,
            "CSRF_ENABLED": False,
        }
    )
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def conn(app):
    with app.app_context():
        connection = get_db()
        yield connection


@pytest.fixture()
def meta(conn):
    """Register two indicators and two templates sharing the header 强度."""
    from warehouse import core

    cur = conn.execute(
        "INSERT INTO indicators (code, name, unit, description, created_at)"
        " VALUES ('STRENGTH_MEASURED', '抗压强度（实测）', 'MPa', '', ?)",
        (core.now_iso(),),
    )
    measured = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO indicators (code, name, unit, description, created_at)"
        " VALUES ('STRENGTH_CONVERTED', '抗压强度（折算）', 'MPa', '', ?)",
        (core.now_iso(),),
    )
    converted = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO indicators (code, name, unit, description, created_at)"
        " VALUES ('SPLIT', '劈裂抗拉强度', 'MPa', '', ?)",
        (core.now_iso(),),
    )
    split = cur.lastrowid

    cur = conn.execute(
        "INSERT INTO templates (name, specimen_header, revision_header, created_at)"
        " VALUES ('实测模板', '试件编号', '修订号', ?)",
        (core.now_iso(),),
    )
    measured_tpl = cur.lastrowid
    conn.execute(
        "INSERT INTO template_columns (template_id, header, indicator_id) VALUES (?, '强度', ?)",
        (measured_tpl, measured),
    )
    conn.execute(
        "INSERT INTO template_columns (template_id, header, indicator_id) VALUES (?, '劈裂', ?)",
        (measured_tpl, split),
    )

    cur = conn.execute(
        "INSERT INTO templates (name, specimen_header, revision_header, created_at)"
        " VALUES ('折算模板', '试件编号', '修订号', ?)",
        (core.now_iso(),),
    )
    converted_tpl = cur.lastrowid
    conn.execute(
        "INSERT INTO template_columns (template_id, header, indicator_id) VALUES (?, '强度', ?)",
        (converted_tpl, converted),
    )

    cur = conn.execute(
        "INSERT INTO templates (name, specimen_header, revision_header, created_at)"
        " VALUES ('补页模板', '试件编号', '修订号', ?)",
        (core.now_iso(),),
    )
    patch_tpl = cur.lastrowid
    conn.execute(
        "INSERT INTO template_columns (template_id, header, indicator_id) VALUES (?, '强度', ?)",
        (patch_tpl, measured),
    )

    conn.commit()
    return {
        "measured": measured,
        "converted": converted,
        "split": split,
        "measured_tpl": measured_tpl,
        "converted_tpl": converted_tpl,
        "patch_tpl": patch_tpl,
    }


@pytest.fixture()
def db(app):
    with app.app_context():
        connection = get_db()
        yield connection
