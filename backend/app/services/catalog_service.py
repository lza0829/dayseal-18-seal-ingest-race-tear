from __future__ import annotations
from typing import Any
from app import scope as scope_rules
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app.repositories import tills as till_repo
from app import align_18 as align_mod


def dashboard() -> dict[str, Any]:
    conn = connect()
    try:
        day = settings_repo.get(conn, "business_day") or ""
        scope = scope_rules.normalize(settings_repo.get(conn, scope_rules.SETTING_KEY))
        summary = line_repo.day_summary(conn, day, scope) if day else None
        # 只认真通过钉扎闭包校验的现行批；半截批不算已封，且问题要在总览可见。
        sealed_batch = close_repo.get_sealed_batch_for_day(conn, day) if day else None
        verdict = (
            close_repo.verify_batch_closure(conn, sealed_batch)
            if sealed_batch
            else None
        )
        sealed = bool(verdict and verdict["ok"])
        payload = {
            "business_day": day,
            "scope": scope,
            "scope_label": scope_rules.label(scope),
            "till_count": len(till_repo.list_tills(conn)),
            "line_count_today": summary["line_count"] if summary else 0,
            "system_total_today": summary["system_total"] if summary else 0.0,
            "cash_total_today": summary["cash_total"] if summary else 0.0,
            "cash_line_count_today": summary["cash_line_count"] if summary else 0,
            "day_line_count_today": summary["day_line_count"] if summary else 0,
            "sealed_today": sealed,
            "closure_ok": verdict["ok"] if verdict else None,
            "closure_problems": verdict["problems"] if verdict else [],
            "close_batch_count": conn.execute("SELECT COUNT(*) c FROM close_batches").fetchone()["c"],
            "open_variance_count": conn.execute(
                "SELECT COUNT(*) c FROM variance_rows WHERE disposition='pending'"
            ).fetchone()["c"],
        }
        return payload
    finally:
        conn.close()


def dashboard_aligned() -> dict:
    raw = dashboard()
    return raw

def dashboard_extended() -> dict:
    from app.db import connect
    from app.repositories import closes as close_repo
    from app import align_18 as align_mod
    base = dashboard()
    conn = connect()
    try:
        batches = close_repo.list_batches(conn)
        return align_mod.overlay_dashboard(base, batches)
    finally:
        conn.close()

