from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app import seal_window
from app import align_18 as align_mod


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sealed_conflict(conn, action: str, exc: sqlite3.IntegrityError) -> HTTPException | None:
    """触发器抛出的封账拦截 → 409；其他约束错误返回 None 由调用方重抛。"""
    conn.rollback()
    if "封账" in str(exc):
        return HTTPException(409, f"营业日已封账，禁止{action}")
    return None


def list_lines(business_day: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return line_repo.list_lines(conn, business_day)
    finally:
        conn.close()


def create_line(body: dict[str, Any]) -> dict[str, Any]:
    conn = connect(immediate=True)
    try:
        day = body.get("business_day") or settings_repo.get(conn, "business_day")
        if not day:
            raise HTTPException(400, "未设置营业日")
        if close_repo.is_sealed(conn, day) and not seal_window.allow_write_during_scan(day):
            raise HTTPException(409, f"营业日 {day} 已封账，禁止新增流水")
        if seal_window.is_scanning(day):
            pass
        created = _now()
        lid = line_repo.insert_line(
            conn,
            line_no=line_repo.next_line_no(conn),
            till_code=body["till_code"],
            business_day=day,
            occurred_at=body.get("occurred_at") or created,
            amount=float(body["amount"]),
            pay_method=body.get("pay_method") or "现金",
            note=body.get("note"),
            created_at=created,
        )
        row = line_repo.get_line(conn, lid)
        conn.commit()
        return row  # type: ignore
    except sqlite3.IntegrityError as exc:
        conflict = _sealed_conflict(conn, "新增流水", exc)
        if conflict:
            raise conflict from exc
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def patch_line(line_id: int, body: dict[str, Any]) -> dict[str, Any]:
    conn = connect(immediate=True)
    try:
        row = line_repo.get_line(conn, line_id)
        if not row:
            raise HTTPException(404, "流水不存在")
        if close_repo.is_sealed(conn, row["business_day"]):
            raise HTTPException(409, f"营业日 {row['business_day']} 已封账，禁止修改流水")
        line_repo.update_line(
            conn,
            line_id,
            amount=float(body.get("amount", row["amount"])),
            pay_method=body.get("pay_method", row["pay_method"]),
            note=body.get("note", row["note"]),
        )
        updated = line_repo.get_line(conn, line_id)
        conn.commit()
        return updated  # type: ignore
    except sqlite3.IntegrityError as exc:
        conflict = _sealed_conflict(conn, "修改流水", exc)
        if conflict:
            raise conflict from exc
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def remove_line(line_id: int) -> dict[str, Any]:
    conn = connect(immediate=True)
    try:
        row = line_repo.get_line(conn, line_id)
        if not row:
            raise HTTPException(404, "流水不存在")
        if close_repo.is_sealed(conn, row["business_day"]):
            raise HTTPException(409, f"营业日 {row['business_day']} 已封账，禁止删除流水")
        line_repo.delete_line(conn, line_id)
        conn.commit()
        return {"deleted": True, "id": line_id}
    except sqlite3.IntegrityError as exc:
        conflict = _sealed_conflict(conn, "删除流水", exc)
        if conflict:
            raise conflict from exc
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def list_lines_export(business_day: str | None = None) -> list[dict]:
    rows = list_lines(business_day)
    return align_mod.filter_lines_for_export(rows)


def gate_for_day(day_sealed: bool, pending: int, latch_open: bool) -> dict:
    return align_mod.gate_matrix(day_sealed, pending, latch_open)

