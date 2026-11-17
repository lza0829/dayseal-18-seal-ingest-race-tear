from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from app import scope as scope_rules
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import settings as settings_repo


def _read(conn) -> str:
    return scope_rules.normalize(settings_repo.get(conn, scope_rules.SETTING_KEY))


def get_scope() -> dict[str, Any]:
    conn = connect()
    try:
        current = _read(conn)
        return {
            "scope": current,
            "label": scope_rules.label(current),
            "description": scope_rules.SCOPE_DESCRIPTIONS[current],
            "options": scope_rules.options(),
        }
    finally:
        conn.close()


def set_scope(body: dict[str, Any]) -> dict[str, Any]:
    try:
        target = scope_rules.parse(body.get("scope"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    conn = connect(immediate=True)
    try:
        previous = _read(conn)
        # 只切换配置；已 sealed 批次是快照，永远不因口径切换而改写。
        settings_repo.set_value(conn, scope_rules.SETTING_KEY, target)
        sealed_count = conn.execute(
            "SELECT COUNT(*) c FROM close_batches WHERE status='sealed'"
        ).fetchone()["c"]
        conn.commit()
        return {
            "scope": target,
            "label": scope_rules.label(target),
            "description": scope_rules.SCOPE_DESCRIPTIONS[target],
            "previous": previous,
            "changed": previous != target,
            "sealed_batch_count": int(sealed_count),
            "options": scope_rules.options(),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
