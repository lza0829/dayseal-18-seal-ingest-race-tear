"""旧库迁移：已有 sealed 批次必须补出口径与现金快照，且不被重复改写。"""
from __future__ import annotations

import sqlite3

import pytest


LEGACY_SCHEMA = '''
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tills (id INTEGER PRIMARY KEY, till_code TEXT NOT NULL UNIQUE, title TEXT NOT NULL);
CREATE TABLE cash_lines (
  id INTEGER PRIMARY KEY, line_no TEXT NOT NULL UNIQUE, till_code TEXT NOT NULL,
  business_day TEXT NOT NULL, occurred_at TEXT NOT NULL, amount REAL NOT NULL,
  pay_method TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE close_batches (
  id INTEGER PRIMARY KEY, batch_no TEXT NOT NULL UNIQUE, business_day TEXT NOT NULL,
  system_total REAL NOT NULL, declared_cash REAL NOT NULL, variance REAL NOT NULL,
  line_count INTEGER NOT NULL, status TEXT NOT NULL, sealed_at TEXT NOT NULL, note TEXT
);
CREATE TABLE variance_rows (
  id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, business_day TEXT NOT NULL,
  variance REAL NOT NULL, disposition TEXT NOT NULL, created_at TEXT NOT NULL
);
'''


@pytest.fixture()
def legacy_db(tmp_path, monkeypatch):
    import app.config as config
    import app.db as db

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / "dayseal.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT INTO settings(key,value) VALUES('business_day','2026-11-16')")
        conn.execute("INSERT INTO tills(till_code,title) VALUES('TILL-A','一号')")
        for no, amount, method in [
            ("CL-1", 100.0, "现金"),
            ("CL-2", 50.0, "微信"),
            ("CL-3", 25.0, "现金"),
        ]:
            conn.execute(
                "INSERT INTO cash_lines(line_no,till_code,business_day,occurred_at,amount,pay_method,created_at) "
                "VALUES(?, 'TILL-A', '2026-11-16', 'x', ?, ?, 'x')",
                (no, amount, method),
            )
        # 旧版封账是“全部流水求和” = 175
        conn.execute(
            "INSERT INTO close_batches(batch_no,business_day,system_total,declared_cash,variance,"
            "line_count,status,sealed_at) VALUES('CB-0001','2026-11-16',175,170,-5,3,'sealed','x')"
        )
        conn.execute(
            "INSERT INTO variance_rows(batch_id,business_day,variance,disposition,created_at) "
            "VALUES(1,'2026-11-16',-5,'pending','x')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def _batch(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        row = dict(conn.execute("SELECT * FROM close_batches WHERE batch_no='CB-0001'").fetchone())
        methods = [
            dict(r)
            for r in conn.execute(
                "SELECT pay_method,total,line_count,included FROM close_batch_methods ORDER BY pay_method"
            ).fetchall()
        ]
        variance = dict(conn.execute("SELECT * FROM variance_rows").fetchone())
        return row, methods, variance
    finally:
        conn.close()


def test_legacy_batch_is_backfilled_and_preserved(legacy_db):
    import app.seed as seed

    seed.seed_if_empty()
    row, methods, variance = _batch(legacy_db)

    # 旧版数字原样保留，口径如实标为全支付方式
    assert row["scope"] == "all_methods"
    assert row["system_total"] == pytest.approx(175.0)
    assert row["variance"] == pytest.approx(-5.0)
    # 补出的现金快照
    assert row["cash_total"] == pytest.approx(125.0)
    assert row["cash_line_count"] == 2
    assert row["day_line_count"] == 3
    # 可核分项
    assert {(m["pay_method"], m["total"], m["included"]) for m in methods} == {
        ("现金", 125.0, 1),
        ("微信", 50.0, 0),
    }
    assert variance["scope"] == "all_methods"

    # 再启动一次不会二次改写
    seed.seed_if_empty()
    row2, methods2, _ = _batch(legacy_db)
    assert row2 == row
    assert methods2 == methods


def test_legacy_then_scope_switch_keeps_batch(legacy_db):
    import app.seed as seed
    from app.db import connect
    from app.services import scope_service

    seed.seed_if_empty()
    before, before_methods, _ = _batch(legacy_db)
    scope_service.set_scope({"scope": "cash_only"})
    after, after_methods, _ = _batch(legacy_db)
    assert before == after
    assert before_methods == after_methods

    conn = connect()
    try:
        assert conn.execute(
            "SELECT value FROM settings WHERE key='close_scope'"
        ).fetchone()["value"] == "cash_only"
    finally:
        conn.close()
