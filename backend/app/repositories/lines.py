from __future__ import annotations
import sqlite3
from typing import Any

from app import scope as scope_rules


def list_lines(conn: sqlite3.Connection, business_day: str | None = None) -> list[dict[str, Any]]:
    if business_day:
        rows = conn.execute(
            "SELECT * FROM cash_lines WHERE business_day=? ORDER BY occurred_at, id",
            (business_day,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM cash_lines ORDER BY business_day DESC, occurred_at, id").fetchall()
    return [dict(r) for r in rows]


def method_breakdown(conn: sqlite3.Connection, business_day: str) -> list[dict[str, Any]]:
    """按支付方式的分项合计。所有支付方式都列出，口径决定 included 标记。"""
    rows = conn.execute(
        '''SELECT pay_method, COALESCE(SUM(amount),0) total, COUNT(*) line_count
             FROM cash_lines WHERE business_day=?
            GROUP BY pay_method ORDER BY pay_method''',
        (business_day,),
    ).fetchall()
    return [
        {
            "pay_method": r["pay_method"],
            "total": round(float(r["total"]), 2),
            "line_count": int(r["line_count"]),
        }
        for r in rows
    ]


def day_summary(conn: sqlite3.Connection, business_day: str, scope: str) -> dict[str, Any]:
    """封账口径下的当日汇总。

    这是系统合计的唯一计算点：封账预览、封账落库、总览、批次回显都调用它，
    不允许在别处另写 SUM。非现金流水不因口径被删改，只是不计入 system_total。
    """
    scope = scope_rules.normalize(scope)

    all_row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) s, COUNT(*) c FROM cash_lines WHERE business_day=?",
        (business_day,),
    ).fetchone()
    cash_row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) s, COUNT(*) c FROM cash_lines "
        "WHERE business_day=? AND pay_method=?",
        (business_day, scope_rules.CASH_PAY_METHOD),
    ).fetchone()

    all_total = round(float(all_row["s"]), 2)
    all_count = int(all_row["c"])
    cash_total = round(float(cash_row["s"]), 2)
    cash_count = int(cash_row["c"])

    if scope == scope_rules.SCOPE_CASH_ONLY:
        system_total, line_count = cash_total, cash_count
    else:
        system_total, line_count = all_total, all_count

    breakdown = method_breakdown(conn, business_day)
    for row in breakdown:
        row["included"] = scope_rules.includes_method(scope, row["pay_method"])

    return {
        "scope": scope,
        "system_total": system_total,
        "line_count": line_count,
        "cash_total": cash_total,
        "cash_line_count": cash_count,
        "all_total": all_total,
        "day_line_count": all_count,
        "methods": breakdown,
    }


def next_line_no(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COUNT(*) c FROM cash_lines").fetchone()["c"] + 1
    return f"CL-{n:04d}"


def insert_line(conn: sqlite3.Connection, **fields: Any) -> int:
    cur = conn.execute(
        '''INSERT INTO cash_lines(line_no, till_code, business_day, occurred_at, amount, pay_method, note, created_at)
           VALUES (?,?,?,?,?,?,?,?)''',
        (
            fields["line_no"],
            fields["till_code"],
            fields["business_day"],
            fields["occurred_at"],
            fields["amount"],
            fields["pay_method"],
            fields.get("note"),
            fields["created_at"],
        ),
    )
    return int(cur.lastrowid)


def update_line(conn: sqlite3.Connection, line_id: int, **fields: Any) -> None:
    conn.execute(
        "UPDATE cash_lines SET amount=?, pay_method=?, note=? WHERE id=?",
        (fields["amount"], fields["pay_method"], fields.get("note"), line_id),
    )


def delete_line(conn: sqlite3.Connection, line_id: int) -> None:
    conn.execute("DELETE FROM cash_lines WHERE id=?", (line_id,))


def get_line(conn: sqlite3.Connection, line_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM cash_lines WHERE id=?", (line_id,)).fetchone()
    return dict(row) if row else None
