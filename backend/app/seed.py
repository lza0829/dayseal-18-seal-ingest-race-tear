from __future__ import annotations
import sqlite3

from app import scope as scope_rules
from app.db import connect


SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tills (
  id INTEGER PRIMARY KEY,
  till_code TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cash_lines (
  id INTEGER PRIMARY KEY,
  line_no TEXT NOT NULL UNIQUE,
  till_code TEXT NOT NULL,
  business_day TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  amount REAL NOT NULL,
  pay_method TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cash_lines_day ON cash_lines(business_day);
CREATE TABLE IF NOT EXISTS close_batches (
  id INTEGER PRIMARY KEY,
  batch_no TEXT NOT NULL UNIQUE,
  business_day TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'cash_only',
  system_total REAL NOT NULL,
  cash_total REAL NOT NULL DEFAULT 0,
  cash_line_count INTEGER NOT NULL DEFAULT 0,
  declared_cash REAL NOT NULL,
  variance REAL NOT NULL,
  line_count INTEGER NOT NULL,
  day_line_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  sealed_at TEXT NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_close_batches_day ON close_batches(business_day);
CREATE TABLE IF NOT EXISTS close_batch_methods (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES close_batches(id) ON DELETE CASCADE,
  pay_method TEXT NOT NULL,
  total REAL NOT NULL,
  line_count INTEGER NOT NULL,
  included INTEGER NOT NULL,
  UNIQUE(batch_id, pay_method)
);
CREATE TABLE IF NOT EXISTS variance_rows (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'cash_only',
  variance REAL NOT NULL,
  disposition TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS variance_holds (
  id INTEGER PRIMARY KEY,
  business_day TEXT NOT NULL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS reopen_requests (
  id INTEGER PRIMARY KEY,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'none'
);
'''

# 封账后该营业日的有效流水禁止增、改、删。
# 服务层已先做校验；触发器是第二道防线，直接改库也会被拒。
GUARDS = '''
CREATE TRIGGER IF NOT EXISTS trg_cash_lines_no_insert_when_sealed
BEFORE INSERT ON cash_lines
WHEN EXISTS (
  SELECT 1 FROM close_batches b
  WHERE b.business_day = NEW.business_day AND b.status = 'sealed'
)
BEGIN
  SELECT RAISE(ABORT, '业务日已封账，禁止新增流水');
END;

CREATE TRIGGER IF NOT EXISTS trg_cash_lines_no_update_when_sealed
BEFORE UPDATE ON cash_lines
WHEN EXISTS (
  SELECT 1 FROM close_batches b
  WHERE b.status = 'sealed' AND b.business_day IN (OLD.business_day, NEW.business_day)
)
BEGIN
  SELECT RAISE(ABORT, '业务日已封账，禁止修改流水');
END;

CREATE TRIGGER IF NOT EXISTS trg_cash_lines_no_delete_when_sealed
BEFORE DELETE ON cash_lines
WHEN EXISTS (
  SELECT 1 FROM close_batches b
  WHERE b.status = 'sealed' AND b.business_day = OLD.business_day
)
BEGIN
  SELECT RAISE(ABORT, '业务日已封账，禁止删除流水');
END;

CREATE TRIGGER IF NOT EXISTS trg_close_batches_lock_snapshot
BEFORE UPDATE OF scope, system_total, cash_total, cash_line_count,
                  declared_cash, variance, line_count, day_line_count,
                  business_day, batch_no, sealed_at
ON close_batches
WHEN OLD.status = 'sealed'
BEGIN
  SELECT RAISE(ABORT, '已封账批次快照不可改写');
END;

CREATE TRIGGER IF NOT EXISTS trg_close_batches_no_delete_when_sealed
BEFORE DELETE ON close_batches
WHEN OLD.status = 'sealed'
BEGIN
  SELECT RAISE(ABORT, '已封账批次不可删除');
END;

CREATE TRIGGER IF NOT EXISTS trg_batch_methods_no_update_when_sealed
BEFORE UPDATE ON close_batch_methods
WHEN EXISTS (
  SELECT 1 FROM close_batches b
  WHERE b.id = OLD.batch_id AND b.status = 'sealed'
)
BEGIN
  SELECT RAISE(ABORT, '已封账批次分项快照不可改写');
END;

CREATE TRIGGER IF NOT EXISTS trg_batch_methods_no_delete_when_sealed
BEFORE DELETE ON close_batch_methods
WHEN EXISTS (
  SELECT 1 FROM close_batches b
  WHERE b.id = OLD.batch_id AND b.status = 'sealed'
)
BEGIN
  SELECT RAISE(ABORT, '已封账批次分项快照不可删除');
END;

CREATE TRIGGER IF NOT EXISTS trg_variance_no_update_when_sealed
BEFORE UPDATE ON variance_rows
WHEN EXISTS (
  SELECT 1 FROM close_batches b
  WHERE b.id = OLD.batch_id AND b.status = 'sealed'
)
BEGIN
  SELECT RAISE(ABORT, '已封账长短款快照不可改写');
END;

CREATE TRIGGER IF NOT EXISTS trg_variance_no_delete_when_sealed
BEFORE DELETE ON variance_rows
WHEN EXISTS (
  SELECT 1 FROM close_batches b
  WHERE b.id = OLD.batch_id AND b.status = 'sealed'
)
BEGIN
  SELECT RAISE(ABORT, '已封账长短款快照不可删除');
END;
'''


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(conn: sqlite3.Connection, table: str, ddl: str, column: str) -> bool:
    """列不存在时补上；返回是否真的新增（用于只跑一次的迁移）。"""
    if column in _columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    return True


def _migrate(conn: sqlite3.Connection) -> None:
    """把旧库补到当前结构；可重复执行。"""
    scope_added = _add_column(
        conn, "close_batches", "scope TEXT NOT NULL DEFAULT 'cash_only'", "scope"
    )
    _add_column(conn, "close_batches", "cash_total REAL NOT NULL DEFAULT 0", "cash_total")
    _add_column(conn, "close_batches", "cash_line_count INTEGER NOT NULL DEFAULT 0", "cash_line_count")
    _add_column(conn, "close_batches", "day_line_count INTEGER NOT NULL DEFAULT 0", "day_line_count")
    _add_column(conn, "variance_rows", "scope TEXT NOT NULL DEFAULT 'cash_only'", "scope")

    if not scope_added:
        return

    # 旧版封账逻辑是“全部流水求和”，因此只在首次补列时把历史批次
    # 如实标注为全支付方式口径，并按当天流水回填现金合计 / 笔数 / 分项，
    # 保证旧批次仍可回溯。
    legacy = conn.execute(
        "SELECT id, business_day FROM close_batches WHERE status='sealed' ORDER BY id"
    ).fetchall()
    for batch in legacy:
        day = batch["business_day"]
        conn.execute(
            "UPDATE close_batches SET scope=?, day_line_count=("
            "  SELECT COUNT(*) FROM cash_lines WHERE business_day=?"
            ") WHERE id=?",
            (scope_rules.SCOPE_ALL_METHODS, day, batch["id"]),
        )
        conn.execute(
            "UPDATE variance_rows SET scope=? WHERE batch_id=?",
            (scope_rules.SCOPE_ALL_METHODS, batch["id"]),
        )
        _backfill_cash_snapshot(conn, batch["id"], day)
        _backfill_method_breakdown(conn, batch["id"], day)


def _backfill_cash_snapshot(conn: sqlite3.Connection, batch_id: int, day: str) -> None:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) s, COUNT(*) c FROM cash_lines "
        "WHERE business_day=? AND pay_method=?",
        (day, scope_rules.CASH_PAY_METHOD),
    ).fetchone()
    conn.execute(
        "UPDATE close_batches SET cash_total=?, cash_line_count=? WHERE id=?",
        (float(row["s"]), int(row["c"]), batch_id),
    )


def _backfill_method_breakdown(conn: sqlite3.Connection, batch_id: int, day: str) -> None:
    rows = conn.execute(
        "SELECT pay_method, COALESCE(SUM(amount),0) s, COUNT(*) c FROM cash_lines "
        "WHERE business_day=? GROUP BY pay_method ORDER BY pay_method",
        (day,),
    ).fetchall()
    for row in rows:
        conn.execute(
            "INSERT OR IGNORE INTO close_batch_methods(batch_id, pay_method, total, line_count, included) "
            "VALUES (?,?,?,?,?)",
            (
                batch_id,
                row["pay_method"],
                float(row["s"]),
                int(row["c"]),
                1 if scope_rules.is_cash(row["pay_method"]) else 0,
            ),
        )


def ensure_scope_setting(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (scope_rules.SETTING_KEY,)
    ).fetchone()
    if row is None or not scope_rules.is_valid(row["value"]):
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (scope_rules.SETTING_KEY, scope_rules.DEFAULT_SCOPE),
        )


def seed_if_empty() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.executescript(GUARDS)
        ensure_scope_setting(conn)
        n = conn.execute("SELECT COUNT(*) c FROM tills").fetchone()["c"]
        if n:
            conn.commit()
            return
        conn.execute("INSERT INTO settings(key, value) VALUES ('business_day', ?)", ("2026-11-16",))
        tills = [
            ("TILL-A", "一号收银"),
            ("TILL-B", "二号收银"),
            ("TILL-C", "三号收银"),
        ]
        for code, title in tills:
            conn.execute("INSERT INTO tills(till_code, title) VALUES (?,?)", (code, title))
        lines = [
            ("CL-0001", "TILL-A", "2026-11-16", "2026-11-16T09:12:00Z", 35.5, "现金", "早市"),
            ("CL-0002", "TILL-A", "2026-11-16", "2026-11-16T10:05:00Z", 128.0, "微信", None),
            ("CL-0003", "TILL-B", "2026-11-16", "2026-11-16T11:20:00Z", 66.0, "现金", None),
            ("CL-0004", "TILL-B", "2026-11-16", "2026-11-16T14:02:00Z", 210.0, "支付宝", None),
            ("CL-0005", "TILL-C", "2026-11-16", "2026-11-16T16:40:00Z", 49.9, "现金", None),
            ("CL-0006", "TILL-A", "2026-11-15", "2026-11-15T18:10:00Z", 88.0, "现金", "昨日"),
        ]
        for row in lines:
            conn.execute(
                '''INSERT INTO cash_lines(line_no, till_code, business_day, occurred_at, amount, pay_method, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (*row, row[3]),
            )
        conn.commit()
    finally:
        conn.close()
