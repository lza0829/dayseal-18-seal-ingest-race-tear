"""封账扫描门与钉扎闭包指纹。

全系统只贯彻一种收口口径：**以封账扫描开始时刻的流水闭包为准**。

* 扫描一开始（``begin_scan``），该营业日的流水闭包即切定，
  之后任何增/改/删一律拒绝，不存在“扫描期间可写”的旁路；
* 进程内门（本模块）负责快速失败，落库门（``seal_state`` + 触发器）
  负责跨连接/跨进程与绕过服务层的直写，两者共用同一时刻语义；
* 批次落库时钉扎闭包成员（``closure_line_ids``）与成员指纹，
  读路径用同一条闭包重算校验，对不上的批次作废、不得当现行。

这不是通用分布式锁原语：状态只表达“某营业日的封账闭包是否已切”。
"""
from __future__ import annotations

import hashlib
import threading


class SealScanConflict(Exception):
    """封账扫描已开始：该营业日流水闭包已切，写入/重复封账一律拒绝。"""


class _SealGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scanning: set[str] = set()

    def begin_scan(self, business_day: str) -> None:
        with self._lock:
            if business_day in self._scanning:
                raise SealScanConflict(f"营业日 {business_day} 封账扫描进行中，禁止重复封账")
            self._scanning.add(business_day)

    def end_scan(self, business_day: str) -> None:
        with self._lock:
            self._scanning.discard(business_day)

    def is_scanning(self, business_day: str) -> bool:
        with self._lock:
            return business_day in self._scanning

    def assert_writable(self, business_day: str) -> None:
        if self.is_scanning(business_day):
            raise SealScanConflict(
                f"营业日 {business_day} 封账扫描已开始，流水闭包已切，禁止写入"
            )

    def reset(self) -> None:
        with self._lock:
            self._scanning.clear()


_GATE = _SealGate()


def begin_scan(business_day: str) -> None:
    _GATE.begin_scan(business_day)


def end_scan(business_day: str) -> None:
    _GATE.end_scan(business_day)


def is_scanning(business_day: str) -> bool:
    return _GATE.is_scanning(business_day)


def assert_writable(business_day: str) -> None:
    """写入口统一调用：扫描闭包已切则抛 SealScanConflict。"""
    _GATE.assert_writable(business_day)


def reset_for_tests() -> None:
    _GATE.reset()


# ---------------------------------------------------------------------------
# 钉扎闭包成员与指纹
# ---------------------------------------------------------------------------

def encode_closure_ids(ids: list[int]) -> str:
    return ",".join(str(i) for i in sorted(int(i) for i in ids))


def parse_closure_ids(encoded: str | None) -> list[int]:
    if not encoded:
        return []
    return [int(part) for part in encoded.split(",") if part]


def fingerprint_for_ids(ids: list[int]) -> str:
    """闭包成员指纹：只认成员集合（排序后的流水 id 列表）。

    合计/笔数可能因口径不同而不同，但同一营业日同一批流水的成员集合唯一，
    因此指纹与口径解耦，专用于发现“集合被悄悄换过”。
    """
    ordered = sorted(int(i) for i in ids)
    digest = hashlib.sha256()
    for line_id in ordered:
        digest.update(str(line_id).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def closure_ids_from_rows(rows) -> list[int]:
    return sorted(int(r["id"]) for r in rows)
