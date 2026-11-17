from __future__ import annotations
import sqlite3
from typing import Any

from app import seal_window


def is_sealed(conn: sqlite3.Connection, business_day: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM close_batches WHERE business_day=? AND status='sealed' LIMIT 1",
        (business_day,),
    ).fetchone()
    return row is not None


# -- 落库封账门 seal_state ----------------------------------------------------

def seal_phase(conn: sqlite3.Connection, business_day: str) -> str | None:
    row = conn.execute(
        "SELECT phase FROM seal_state WHERE business_day=?", (business_day,)
    ).fetchone()
    return row["phase"] if row else None


def begin_scan_state(
    conn: sqlite3.Connection, business_day: str, started_at: str
) -> None:
    """切扫描闭包。已有门（扫描中或已封）即冲突，由调用方转 409。"""
    try:
        conn.execute(
            "INSERT INTO seal_state(business_day, phase, batch_id, updated_at) "
            "VALUES (?, 'scanning', NULL, ?)",
            (business_day, started_at),
        )
    except sqlite3.IntegrityError as exc:
        raise seal_window.SealScanConflict(
            f"营业日 {business_day} 封账扫描已开始或已封账"
        ) from exc


def mark_sealed_state(
    conn: sqlite3.Connection, business_day: str, batch_id: int, sealed_at: str
) -> None:
    conn.execute(
        "UPDATE seal_state SET phase='sealed', batch_id=?, updated_at=? "
        "WHERE business_day=?",
        (batch_id, sealed_at, business_day),
    )


def clear_seal_state(conn: sqlite3.Connection, business_day: str) -> None:
    conn.execute("DELETE FROM seal_state WHERE business_day=?", (business_day,))


# -- 钉扎闭包 -----------------------------------------------------------------

def day_line_ids(conn: sqlite3.Connection, business_day: str) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM cash_lines WHERE business_day=? ORDER BY id",
        (business_day,),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def closure_summary(conn: sqlite3.Connection, line_ids: list[int]) -> list[sqlite3.Row]:
    if not line_ids:
        return []
    placeholders = ",".join("?" for _ in line_ids)
    return conn.execute(
        f"SELECT id, amount, pay_method FROM cash_lines WHERE id IN ({placeholders})",
        tuple(line_ids),
    ).fetchall()


def next_batch_no(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COUNT(*) c FROM close_batches").fetchone()["c"] + 1
    return f"CB-{n:04d}"


def create_batch(conn: sqlite3.Connection, **fields: Any) -> int:
    cur = conn.execute(
        '''INSERT INTO close_batches(
             batch_no, business_day, scope, system_total, cash_total, cash_line_count,
             declared_cash, variance, line_count, day_line_count, status, sealed_at, note,
             scan_started_at, closure_line_ids, fingerprint
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
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
            fields["scan_started_at"],
            fields["closure_line_ids"],
            fields["fingerprint"],
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


def get_sealed_batch_for_day(
    conn: sqlite3.Connection, business_day: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM close_batches WHERE business_day=? AND status='sealed' "
        "ORDER BY id LIMIT 1",
        (business_day,),
    ).fetchone()
    return dict(row) if row else None


def verify_batch_closure(
    conn: sqlite3.Connection, batch: dict[str, Any]
) -> dict[str, Any]:
    """按批次钉扎的闭包成员重算，核对“合计/笔数=钉扎闭包、指纹=成员集合”。

    任何一项对不上都说明出现了半截钉扎（锁后账外行 / 指纹旧快照 / 集合漂移），
    该 sealed 批不得当现行，由调用方作废后按闭包重新封账。
    """
    from app import scope as scope_rules

    problems: list[str] = []
    pinned_ids = seal_window.parse_closure_ids(batch.get("closure_line_ids"))
    rows = closure_summary(conn, pinned_ids)
    existing_ids = {int(r["id"]) for r in rows}

    missing = sorted(set(pinned_ids) - existing_ids)
    if missing:
        problems.append(f"钉扎成员已缺失: {missing}")

    # 现行库内同营业日流水必须恰好等于钉扎集合：多一笔即锁后账外行。
    day_ids = day_line_ids(conn, batch["business_day"])
    stray = sorted(set(day_ids) - set(pinned_ids))
    if stray:
        problems.append(f"存在封账后账外有效流水: {stray}")

    expect_fp = seal_window.fingerprint_for_ids(pinned_ids)
    if batch.get("fingerprint") != expect_fp:
        problems.append("批次成员指纹与钉扎清单不一致")

    scope = scope_rules.normalize(batch.get("scope"))
    included_rows = [
        r for r in rows if scope_rules.includes_method(scope, r["pay_method"])
    ]
    system_total = round(sum(float(r["amount"]) for r in included_rows), 2)
    line_count = len(included_rows)
    cash_rows = [r for r in rows if r["pay_method"] == scope_rules.CASH_PAY_METHOD]
    cash_total = round(sum(float(r["amount"]) for r in cash_rows), 2)
    cash_line_count = len(cash_rows)

    if abs(float(batch["system_total"]) - system_total) > 1e-9:
        problems.append(
            f"系统合计 {batch['system_total']} != 钉扎闭包重算 {system_total}"
        )
    if int(batch["line_count"]) != line_count:
        problems.append(
            f"计入笔数 {batch['line_count']} != 钉扎闭包重算 {line_count}"
        )
    if abs(float(batch["cash_total"]) - cash_total) > 1e-9:
        problems.append(
            f"现金合计 {batch['cash_total']} != 钉扎闭包重算 {cash_total}"
        )
    if int(batch["cash_line_count"]) != cash_line_count:
        problems.append(
            f"现金笔数 {batch['cash_line_count']} != 钉扎闭包重算 {cash_line_count}"
        )
    if int(batch["day_line_count"]) != len(day_ids):
        problems.append(
            f"当日笔数 {batch['day_line_count']} != 现行流水笔数 {len(day_ids)}"
        )

    return {
        "ok": not problems,
        "problems": problems,
        "pinned_count": len(pinned_ids),
        "current_count": len(day_ids),
        "recomputed_total": system_total,
        "recomputed_line_count": line_count,
        "fingerprint": expect_fp,
    }


def void_batch(conn: sqlite3.Connection, batch_id: int, reason: str, at: str) -> None:
    """作废半截/漂移批次并解除该营业日落库门，恢复可写以便按闭包重封。

    触发器只锁数字快照列，status/note 允许流转；作废后其余 sealed 触发器
    因不再存在该日 sealed 行而自然解除。
    """
    conn.execute(
        "UPDATE close_batches SET status='void', "
        "note=COALESCE(note,'') || ? WHERE id=? AND status='sealed'",
        (f"|void@{at}:{reason}", batch_id),
    )
    day = conn.execute(
        "SELECT business_day FROM close_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if day is not None:
        clear_seal_state(conn, day["business_day"])

