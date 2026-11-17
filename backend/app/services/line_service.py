from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException
from app.db import connect
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app import seal_window
from app import align_18 as align_mod
from app.services import close_service


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sealed_conflict(conn, action: str, exc: sqlite3.IntegrityError) -> HTTPException | None:
    """触发器/落库门抛出的封账拦截 → 409；其他约束错误返回 None 由调用方重抛。"""
    conn.rollback()
    message = str(exc)
    if "封账" in message or "封账扫描" in message:
        return HTTPException(409, f"营业日已封账或封账扫描已开始，禁止{action}")
    return None


def list_lines(business_day: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return line_repo.list_lines(conn, business_day)
    finally:
        conn.close()


def create_line(body: dict[str, Any]) -> dict[str, Any]:
    # 进程门先于写事务：扫描闭包已切的营业日在抢锁前就 409，
    # 不打开写事务、不产生行号，避免与封账事务在锁上交错。
    day = body.get("business_day")
    if day:
        try:
            seal_window.assert_writable(day)
        except seal_window.SealScanConflict as exc:
            raise HTTPException(409, str(exc)) from exc
    conn = connect(immediate=True)
    try:
        day = day or settings_repo.get(conn, "business_day")
        if not day:
            raise HTTPException(400, "未设置营业日")
        seal_window.assert_writable(day)
        # 写事务内总序收口：健康 sealed 批 409；半截批作废后放行。
        # seal_state 触发器是同一时刻语义的落库兜底（直连改库也拒）。
        close_service.ensure_day_writable(conn, day, "新增流水")
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
    except seal_window.SealScanConflict as exc:
        conn.rollback()
        raise HTTPException(409, str(exc)) from exc
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
        day = row["business_day"]
        seal_window.assert_writable(day)
        close_service.ensure_day_writable(conn, day, "修改流水")
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
    except seal_window.SealScanConflict as exc:
        conn.rollback()
        raise HTTPException(409, str(exc)) from exc
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
        day = row["business_day"]
        seal_window.assert_writable(day)
        close_service.ensure_day_writable(conn, day, "删除流水")
        line_repo.delete_line(conn, line_id)
        conn.commit()
        return {"deleted": True, "id": line_id}
    except seal_window.SealScanConflict as exc:
        conn.rollback()
        raise HTTPException(409, str(exc)) from exc
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

