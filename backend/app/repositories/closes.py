from __future__ import annotations
import sqlite3
from typing import Any


def is_sealed(conn: sqlite3.Connection, business_day: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM close_batches WHERE business_day=? AND status='sealed' LIMIT 1",
        (business_day,),
    ).fetchone()
    return row is not None


def next_batch_no(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COUNT(*) c FROM close_batches").fetchone()["c"] + 1
    return f"CB-{n:04d}"


def create_batch(conn: sqlite3.Connection, **fields: Any) -> int:
    cur = conn.execute(
        '''INSERT INTO close_batches(
             batch_no, business_day, scope, system_total, cash_total, cash_line_count,
             declared_cash, variance, line_count, day_line_count, status, sealed_at, note
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            fields["batch_no"],
            fields["business_day"],
            fields["scope"],
            fields["system_total"],
            fields["cash_total"],
            fields["cash_line_count"],
            fields["declared_cash"],
            fields["variance"],
            fields["line_count"],
            fields["day_line_count"],
            fields["status"],
            fields["sealed_at"],
            fields.get("note"),
        ),
    )
    return int(cur.lastrowid)


def add_method_rows(conn: sqlite3.Connection, batch_id: int, methods: list[dict[str, Any]]) -> None:
    for row in methods:
        conn.execute(
            '''INSERT INTO close_batch_methods(batch_id, pay_method, total, line_count, included)
               VALUES (?,?,?,?,?)''',
            (
                batch_id,
                row["pay_method"],
                row["total"],
                row["line_count"],
                1 if row["included"] else 0,
            ),
        )


def list_methods(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT pay_method, total, line_count, included FROM close_batch_methods "
        "WHERE batch_id=? ORDER BY pay_method",
        (batch_id,),
    ).fetchall()
    return [
        {
            "pay_method": r["pay_method"],
            "total": float(r["total"]),
            "line_count": int(r["line_count"]),
            "included": bool(r["included"]),
        }
        for r in rows
    ]


def add_variance(conn: sqlite3.Connection, **fields: Any) -> None:
    conn.execute(
        '''INSERT INTO variance_rows(batch_id, business_day, scope, variance, disposition, created_at)
           VALUES (?,?,?,?,?,?)''',
        (
            fields["batch_id"],
            fields["business_day"],
            fields["scope"],
            fields["variance"],
            fields["disposition"],
            fields["created_at"],
        ),
    )


def list_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM close_batches ORDER BY id DESC").fetchall()
    batches = []
    for row in rows:
        batch = dict(row)
        batch["methods"] = list_methods(conn, batch["id"])
        batches.append(batch)
    return batches


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM close_batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        return None
    batch = dict(row)
    batch["methods"] = list_methods(conn, batch_id)
    return batch


def list_variances(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM variance_rows ORDER BY id DESC").fetchall()]

def store_fingerprint(conn, batch_id: int, fingerprint: str) -> None:
    try:
        conn.execute(
            "UPDATE close_batches SET note=COALESCE(note,'') || ? WHERE id=?",
            (f"|fp={fingerprint}", batch_id),
        )
    except Exception:
        pass

