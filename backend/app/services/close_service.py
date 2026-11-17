from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException
from app import scope as scope_rules
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app import seal_window
from app import align_18 as align_mod


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _current_scope(conn) -> str:
    return scope_rules.normalize(settings_repo.get(conn, scope_rules.SETTING_KEY))


def _verify_day_sealed(conn, day: str):
    """返回 (现行 sealed 批, 闭包校验结论)；无 sealed 批则两个都为 None。"""
    batch = close_repo.get_sealed_batch_for_day(conn, day)
    if not batch:
        return None, None
    return batch, close_repo.verify_batch_closure(conn, batch)


def _annotate_batch(conn, batch: dict[str, Any]) -> dict[str, Any]:
    """读路径统一标注闭包校验结论；只读，不产生副作用。"""
    if str(batch.get("status")) == "sealed":
        verdict = close_repo.verify_batch_closure(conn, batch)
        batch["closure_ok"] = verdict["ok"]
        batch["closure_problems"] = verdict["problems"]
    else:
        batch["closure_ok"] = None
        batch["closure_problems"] = []
    return batch


def ensure_day_writable(conn, day: str, action: str) -> None:
    """流水写入口的封账收口（在 BEGIN IMMEDIATE 事务内调用）。

    健康 sealed 批 → 409；半截批 → 当场作废、恢复可写后放行，
    作废原因随批次留档，不会出现“锁在账外无解释”的有效行。
    """
    batch, verdict = _verify_day_sealed(conn, day)
    if batch and verdict and verdict["ok"]:
        raise HTTPException(409, f"营业日 {day} 已封账，禁止{action}")
    if batch and verdict:
        close_repo.void_batch(conn, batch["id"], "；".join(verdict["problems"]), _now())


def preview(business_day: str | None = None) -> dict[str, Any]:
    conn = connect()
    try:
        day = business_day or settings_repo.get(conn, "business_day") or ""
        scope = _current_scope(conn)
        summary = line_repo.day_summary(conn, day, scope)
        return {
            "business_day": day,
            "scope": scope,
            "scope_label": scope_rules.label(scope),
            "scope_description": scope_rules.SCOPE_DESCRIPTIONS[scope],
            "scope_options": scope_rules.options(),
            "system_total": summary["system_total"],
            "line_count": summary["line_count"],
            "cash_total": summary["cash_total"],
            "cash_line_count": summary["cash_line_count"],
            "day_line_count": summary["day_line_count"],
            "all_total": summary["all_total"],
            "methods": summary["methods"],
            "lines": line_repo.list_lines(conn, day),
        } | _preview_seal_state(conn, day)
    finally:
        conn.close()


def _preview_seal_state(conn, day: str) -> dict[str, Any]:
    """预览页的封账态只认真通过闭包校验的现行批；半截批显式暴露问题。"""
    batch, verdict = _verify_day_sealed(conn, day)
    if not batch:
        return {
            "sealed": False,
            "scanning": close_repo.seal_phase(conn, day) == "scanning",
            "closure_ok": None,
            "closure_problems": [],
        }
    return {
        "sealed": bool(verdict and verdict["ok"]),
        "scanning": close_repo.seal_phase(conn, day) == "scanning",
        "sealed_batch_id": batch["id"],
        "closure_ok": verdict["ok"],
        "closure_problems": verdict["problems"],
        "fingerprint": batch.get("fingerprint"),
    }


def seal(body: dict[str, Any]) -> dict[str, Any]:
    day = body.get("business_day")
    if not day:
        # 读一次设置补默认营业日，不切任何封账门。
        conn = connect()
        try:
            day = settings_repo.get(conn, "business_day")
        finally:
            conn.close()
    if not day:
        raise HTTPException(400, "未指定营业日")
    if "declared_cash" not in body:
        raise HTTPException(400, "未填写实盘现金")

    # 进程门先于写事务：扫描闭包一旦切定，同营业日的并发封账在抢锁前就 409，
    # 不会出现第二封堵在写锁上、却没被门收口的交错。
    try:
        seal_window.begin_scan(day)
    except seal_window.SealScanConflict as exc:
        raise HTTPException(409, str(exc)) from exc

    conn = connect(immediate=True)
    try:
        # BEGIN IMMEDIATE 已取写锁：封账事务与流水写事务在 SQLite 层总序，
        # 谁先取到锁谁先切闭包，不存在两套闭包各封一份的交错。
        existing, verdict = _verify_day_sealed(conn, day)
        if existing and verdict and verdict["ok"]:
            raise HTTPException(409, f"营业日 {day} 已封账")
        if existing and verdict:
            # 半截指纹批不得现行：先作废再按现行闭包重封。
            close_repo.void_batch(conn, existing["id"], "；".join(verdict["problems"]), _now())

        scan_started_at = _now()
        # 落库门：从这一行提交前的事务内可见点起，触发器即拒该日一切流水改动。
        # 写锁保证此期间没有任何其他写事务能插进来。
        close_repo.begin_scan_state(conn, day, scan_started_at)

        # 口径在写事务内读取，与下面的合计同源，避免与并发切换口径交错。
        # 封账永远贯彻当前全局口径，不接受逐次覆盖。
        scope = _current_scope(conn)
        declared = float(body["declared_cash"])

        # 钉扎扫描开始时刻的流水闭包：成员清单 + 成员指纹随批留档。
        closure_ids = close_repo.day_line_ids(conn, day)
        fingerprint = seal_window.fingerprint_for_ids(closure_ids)
        summary = line_repo.day_summary(conn, day, scope)
        total = summary["system_total"]
        variance = round(declared - total, 2)
        sealed_at = _now()

        batch_id = close_repo.create_batch(
            conn,
            batch_no=close_repo.next_batch_no(conn),
            business_day=day,
            scope=scope,
            system_total=total,
            cash_total=summary["cash_total"],
            cash_line_count=summary["cash_line_count"],
            declared_cash=declared,
            variance=variance,
            line_count=summary["line_count"],
            day_line_count=summary["day_line_count"],
            status="sealed",
            sealed_at=sealed_at,
            note=body.get("note"),
            scan_started_at=scan_started_at,
            closure_line_ids=seal_window.encode_closure_ids(closure_ids),
            fingerprint=fingerprint,
        )
        # 全支付方式口径必须写出可核的分项合计；仅现金口径同样留档，
        # 便于事后核查批次到底排除了哪些支付方式。
        close_repo.add_method_rows(conn, batch_id, summary["methods"])

        disposition = "pending" if abs(variance) > 1e-9 else "balanced"
        close_repo.add_variance(
            conn,
            batch_id=batch_id,
            business_day=day,
            scope=scope,
            variance=variance,
            disposition=disposition,
            created_at=sealed_at,
        )
        # 落库门转 sealed，与批次同事务提交；触发器在提交前已生效。
        close_repo.mark_sealed_state(conn, day, batch_id, sealed_at)
        conn.commit()
        return {
            "batch_id": batch_id,
            "business_day": day,
            "scope": scope,
            "scope_label": scope_rules.label(scope),
            "system_total": total,
            "cash_total": summary["cash_total"],
            "cash_line_count": summary["cash_line_count"],
            "declared_cash": declared,
            "variance": variance,
            "line_count": summary["line_count"],
            "day_line_count": summary["day_line_count"],
            "status": "sealed",
            "disposition": disposition,
            "scan_started_at": scan_started_at,
            "closure_line_count": len(closure_ids),
            "fingerprint": fingerprint,
            "closure_ok": True,
            "closure_problems": [],
            "methods": close_repo.list_methods(conn, batch_id),
        }
    except seal_window.SealScanConflict as exc:
        conn.rollback()
        raise HTTPException(409, str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        # seal_state / 单现行批次唯一索引等落库门冲突，统一 409。
        conn.rollback()
        raise HTTPException(409, f"营业日 {day} 封账冲突：{exc}") from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        seal_window.end_scan(day)


def list_batches() -> list[dict[str, Any]]:
    conn = connect()
    try:
        batches = close_repo.list_batches(conn)
        return [_annotate_batch(conn, batch) for batch in batches]
    finally:
        conn.close()


def get_batch(batch_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        batch = close_repo.get_batch(conn, batch_id)
        if not batch:
            raise HTTPException(404, "封账批次不存在")
        batch["scope_label"] = scope_rules.label(batch["scope"])
        return _annotate_batch(conn, batch)
    finally:
        conn.close()


def void_batch(batch_id: int, reason: str | None = None) -> dict[str, Any]:
    """显式作废通不过闭包校验的半截批，解除日锁并恢复可写。

    健康 sealed 批不允许从这里作废；半截批作废后方可按现行闭包重封。
    """
    conn = connect(immediate=True)
    try:
        batch = close_repo.get_batch(conn, batch_id)
        if not batch:
            raise HTTPException(404, "封账批次不存在")
        if str(batch.get("status")) != "sealed":
            raise HTTPException(409, "只有现行 sealed 批次可作废")
        verdict = close_repo.verify_batch_closure(conn, batch)
        if verdict["ok"]:
            raise HTTPException(409, "批次闭包校验通过，禁止作废健康批次")
        close_repo.void_batch(
            conn, batch_id, reason or "；".join(verdict["problems"]), _now()
        )
        conn.commit()
        return {"voided": True, "batch_id": batch_id, "problems": verdict["problems"]}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_variances() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return close_repo.list_variances(conn)
    finally:
        conn.close()

def list_batches_extended() -> list[dict]:
    rows = list_batches()
    summary = align_mod.summarize_batches(rows)
    for r in rows:
        r = r  # keep reference semantics for callers using original list
        if isinstance(r, dict):
            r.setdefault("alignment", summary.get("alignment_token") if False else summary.get("sealed_count"))
    return rows


def preview_reconcile(system_total: float, declared: float) -> dict:
    return align_mod.reconcile_hint(system_total, declared)

