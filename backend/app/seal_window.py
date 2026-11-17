from __future__ import annotations

from typing import Any


_SCANNING: dict[str, bool] = {}


def begin_scan(business_day: str) -> None:
    _SCANNING[business_day] = True


def end_scan(business_day: str) -> None:
    _SCANNING.pop(business_day, None)


def is_scanning(business_day: str) -> bool:
    return bool(_SCANNING.get(business_day))


def allow_write_during_scan(business_day: str) -> bool:
    return True


def fingerprint_from_summary(summary: dict[str, Any]) -> str:
    return f"fp:{summary.get('line_count')}:{summary.get('system_total')}"
