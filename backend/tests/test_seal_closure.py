"""封账钉扎时刻 ↔ 流水写入时刻分叉的互斥收口测试。

全系统只贯彻一种口径：以封账扫描开始时刻的流水闭包为准。

三组核心夹具互证，外加真实多线程并发与落库门/迁移兜底：

1. 锁后账外行：扫描开始后写入不得既进库又被锁在账外——一律 409，
   且 sealed 现行批的合计/笔数/成员指纹等于其钉扎闭包；
2. 合计含锁后写但指纹旧：人为造出的半截批通不过闭包校验、不得现行，
   写入口/重封先作废它，作废留痕后按现行闭包重封；
3. 双封集合不一：同营业日并发封账只有一份现行批，第二个 409，
   并发写入要么在扫描前进闭包、要么 409，不存在账外有效行。
"""
from __future__ import annotations

import sqlite3
import threading
import warnings

import pytest
from fastapi.testclient import TestClient

from app import seal_window

DAY = "2026-11-16"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app.config as config
    import app.seed as seed
    import app.db as db
    import app.seal_window as seal_window

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "dayseal.db")
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "dayseal.db")

    seal_window.reset_for_tests()
    seed.seed_if_empty()

    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as c:
        yield c
    seal_window.reset_for_tests()


def _seal(client, day=DAY, declared=151.4):
    return client.post(
        "/api/close/seal", json={"business_day": day, "declared_cash": declared}
    )


def _raw_conn():
    from app.config import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _drop_and_restore_seal_triggers(conn):
    """直连造半截批时临时卸下封账门触发器，造完原样装回。"""
    names = [
        "trg_cash_lines_no_insert_during_seal",
        "trg_cash_lines_no_update_during_seal",
        "trg_cash_lines_no_delete_during_seal",
        "trg_cash_lines_no_insert_when_sealed",
        "trg_cash_lines_no_update_when_sealed",
        "trg_cash_lines_no_delete_when_sealed",
    ]
    for name in names:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    return names


def _recreate_seal_triggers(conn):
    from app.seed import GUARDS

    conn.executescript(GUARDS)


# ---------------------------------------------------------------------------
# 夹具一：锁后账外行 —— 扫描开始后的写入一律拒绝，不产生账外有效行
# ---------------------------------------------------------------------------

def test_writes_during_scan_window_are_rejected(client, monkeypatch):
    # 种子：DAY 有 5 笔流水，现金 3 笔合计 151.4
    assert len(client.get(f"/api/lines?business_day={DAY}").json()) == 5

    import app.services.close_service as close_svc
    import app.services.line_service as line_svc
    import app.seal_window as seal_window
    from fastapi import HTTPException

    real_now = close_svc._now
    write_attempted = threading.Event()

    def slow_now():
        # 在扫描闭包切定之后、批次落库之前制造一个写入尝试（同进程服务层直调，
        # 不走 HTTP，避免 TestClient 同线程嵌套）。进程门可抛门控原语或已转译的 409，
        # 两者语义相同：扫描开始后的写入被拒。
        if seal_window.is_scanning(DAY) and not write_attempted.is_set():
            write_attempted.set()
            with pytest.raises((seal_window.SealScanConflict, HTTPException)) as info:
                line_svc.create_line(
                    {"business_day": DAY, "till_code": "TILL-A", "amount": 777.0}
                )
            exc = info.value
            assert getattr(exc, "status_code", 409) == 409
        return real_now()

    monkeypatch.setattr(close_svc, "_now", slow_now)

    r = _seal(client)
    assert r.status_code == 200, r.text
    assert write_attempted.is_set()
    batch = r.json()
    assert batch["closure_line_count"] == 5
    assert batch["line_count"] == 3
    assert batch["system_total"] == pytest.approx(151.4)
    assert batch["closure_ok"] is True

    # 那笔 777 既没进库，也不存在“锁在账外”的解释缺口
    rows = client.get(f"/api/lines?business_day={DAY}").json()
    assert len(rows) == 5
    assert all(row["amount"] != 777.0 for row in rows)

    # 封账后写入仍然 409
    r = client.post(
        "/api/lines",
        json={"business_day": DAY, "till_code": "TILL-A", "amount": 1.0},
    )
    assert r.status_code == 409


def test_seal_state_and_triggers_block_bypass_writes(client):
    """绕过服务层直连写库：seal_state + 触发器是同一时刻语义的落库防线。"""
    assert _seal(client).status_code == 200

    conn = _raw_conn()
    try:
        # 触发器拦
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cash_lines(line_no, till_code, business_day, occurred_at, "
                "amount, pay_method, created_at) VALUES "
                "('CL-BYPASS','TILL-A',?,'x',1.0,'现金','x')",
                (DAY,),
            )
        # seal_state 确实处在 sealed 态
        phase = conn.execute(
            "SELECT phase FROM seal_state WHERE business_day=?", (DAY,)
        ).fetchone()
        assert phase is not None and phase["phase"] == "sealed"

        # 即使触发器被人卸掉，seal_state 仍是可核查的事实来源；
        # 重启重建会把它恢复回来
    finally:
        conn.close()


def test_scanning_state_persists_in_tx_and_blocks_after_commit(tmp_path, monkeypatch):
    """扫描态在封账事务内落库，提交后触发器立即接管该营业日。

    事务进行中其他连接被写锁挡在外面（SQLite 写事务总序），
    因此扫描态一旦提交，没有任何写入能插进“扫描开始→落锁”的窗口。
    """
    import app.config as config
    import app.seed as seed
    import app.db as db
    import app.seal_window as seal_window
    from app.db import connect
    from app.repositories import closes as close_repo

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "dayseal.db")
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "dayseal.db")
    seal_window.reset_for_tests()
    seed.seed_if_empty()

    sealer = connect(immediate=True)
    try:
        close_repo.begin_scan_state(sealer, DAY, "2026-11-16T23:59:00Z")
        assert close_repo.seal_phase(sealer, DAY) == "scanning"
        sealer.commit()
    finally:
        sealer.close()

    # 提交后：另一连接的直写撞 seal_state 触发器，且不需要任何服务层配合
    other = sqlite3.connect(str(db.DB_PATH), timeout=1.0)
    try:
        with pytest.raises(sqlite3.IntegrityError) as info:
            other.execute(
                "INSERT INTO cash_lines(line_no, till_code, business_day, occurred_at, "
                "amount, pay_method, created_at) VALUES "
                "('CL-X','TILL-A',?,'x',1.0,'现金','x')",
                (DAY,),
            )
        assert "封账扫描" in str(info.value)
    finally:
        other.close()


# ---------------------------------------------------------------------------
# 夹具二：合计含锁后写但指纹旧 —— 半截指纹批不得现行
# ---------------------------------------------------------------------------

def _seal_then_inject_stray_line(client) -> int:
    """封账后直连注入一笔账外有效行，造出“合计不含它、库里却有它”的半截批。"""
    r = _seal(client)
    assert r.status_code == 200, r.text
    batch_id = r.json()["batch_id"]

    conn = _raw_conn()
    try:
        names = _drop_and_restore_seal_triggers(conn)
        try:
            conn.execute(
                "INSERT INTO cash_lines(line_no, till_code, business_day, occurred_at, "
                "amount, pay_method, created_at) VALUES "
                "('CL-STRAY','TILL-A',?,'x',999.0,'现金','x')",
                (DAY,),
            )
            conn.commit()
        finally:
            _recreate_seal_triggers(conn)
        assert names  # 触发器已装回
    finally:
        conn.close()
    return batch_id


def test_half_pinned_batch_fails_closure_and_is_not_current(client):
    batch_id = _seal_then_inject_stray_line(client)

    # 批次读路径显式暴露闭包问题，且不再算“已封”
    detail = client.get(f"/api/close/batches/{batch_id}").json()
    assert detail["closure_ok"] is False
    assert any("账外有效流水" in p for p in detail["closure_problems"])

    preview = client.get(f"/api/close/preview?business_day={DAY}").json()
    assert preview["sealed"] is False
    assert preview["closure_ok"] is False
    assert any("账外有效流水" in p for p in preview["closure_problems"])

    dash = client.get("/api/dashboard").json()
    assert dash["sealed_today"] is False
    assert dash["closure_ok"] is False


def test_write_entry_voids_half_pinned_batch_with_explanation(client):
    batch_id = _seal_then_inject_stray_line(client)

    # 写入口遇到半截批：作废后放行，绝不把新行再锁在账外
    r = client.post(
        "/api/lines",
        json={"business_day": DAY, "till_code": "TILL-B", "amount": 12.0},
    )
    assert r.status_code == 200, r.text

    detail = client.get(f"/api/close/batches/{batch_id}").json()
    assert detail["status"] == "void"
    # 作废原因留档，账外行问题有解释
    assert "void@" in (detail["note"] or "")
    assert "账外有效流水" in (detail["note"] or "")

    # 日锁随作废解除，seal_state 一并清掉
    conn = _raw_conn()
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM seal_state WHERE business_day=?", (DAY,)
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_reseal_after_void_pins_current_closure(client):
    _seal_then_inject_stray_line(client)

    # 重封：半截批先作废，新批按现行闭包（含 stray 行）钉扎
    r = _seal(client, declared=1162.4)
    assert r.status_code == 200, r.text
    new_batch = r.json()
    # 原 5 笔 + stray 999 = 6 笔成员
    assert new_batch["closure_line_count"] == 6
    # 现金闭包：151.4 + 999 = 1150.4
    assert new_batch["system_total"] == pytest.approx(1150.4)
    assert new_batch["closure_ok"] is True

    batches = client.get("/api/close/batches").json()
    statuses = {b["id"]: b["status"] for b in batches}
    assert new_batch["batch_id"] in statuses
    sealed_batches = [b for b in batches if b["status"] == "sealed"]
    assert len(sealed_batches) == 1
    assert sealed_batches[0]["closure_ok"] is True


def test_stale_fingerprint_batch_is_detected(client):
    """直接篡改指纹（成员集合没变但指纹是旧快照）：同样不得现行。"""
    batch_id = _seal(client).json()["batch_id"]
    conn = _raw_conn()
    try:
        # 快照触发器是正确行为；模拟直连破坏时先卸触发器，改完装回
        conn.execute("DROP TRIGGER IF EXISTS trg_close_batches_lock_snapshot")
        conn.execute(
            "UPDATE close_batches SET fingerprint='fp:stale-snapshot' WHERE id=?",
            (batch_id,),
        )
        conn.commit()
    finally:
        from app.seed import GUARDS

        conn.executescript(GUARDS)
        conn.close()

    detail = client.get(f"/api/close/batches/{batch_id}").json()
    assert detail["closure_ok"] is False
    assert any("指纹" in p for p in detail["closure_problems"])

    # 该批已不健康（指纹坏），显式作废成功
    r = client.post(f"/api/close/batches/{batch_id}/void", json={})
    assert r.status_code == 200, r.text


def test_healthy_batch_cannot_be_voided(client):
    batch_id = _seal(client).json()["batch_id"]
    r = client.post(f"/api/close/batches/{batch_id}/void", json={})
    assert r.status_code == 409
    detail = client.get(f"/api/close/batches/{batch_id}").json()
    assert detail["status"] == "sealed"


# ---------------------------------------------------------------------------
# 夹具三：双封集合不一 —— 同营业日只有一份现行批
# ---------------------------------------------------------------------------

def test_second_seal_conflicts_and_no_duplicate_current_batch(client):
    first = _seal(client)
    assert first.status_code == 200
    second = _seal(client)
    assert second.status_code == 409

    conn = _raw_conn()
    try:
        sealed = conn.execute(
            "SELECT COUNT(*) c FROM close_batches WHERE business_day=? AND status='sealed'",
            (DAY,),
        ).fetchone()["c"]
        assert sealed == 1
        states = conn.execute(
            "SELECT COUNT(*) c FROM seal_state WHERE business_day=?", (DAY,)
        ).fetchone()["c"]
        assert states == 1
    finally:
        conn.close()


def test_concurrent_seal_and_writes_leave_no_off_book_rows(client):
    """真实多线程：封账与若干并发写入交错，只允许一种闭包结果。"""
    results: list[tuple[str, int]] = []
    barrier = threading.Barrier(3)

    def writer(idx):
        barrier.wait()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", pytest.PytestUnhandledThreadExceptionWarning)
            try:
                r = client.post(
                    "/api/lines",
                    json={
                        "business_day": DAY,
                        "till_code": "TILL-A",
                        "amount": 100.0 + idx,
                        "pay_method": "现金",
                    },
                )
                results.append((f"w{idx}", r.status_code))
            except seal_window.SealScanConflict:
                # 线程内透传的门控原语等价于该写入被 409 拒绝
                results.append((f"w{idx}", 409))

    def sealer():
        barrier.wait()
        r = _seal(client, declared=1000.0)
        results.append(("seal", r.status_code))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
    threads.append(threading.Thread(target=sealer))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    seal_results = [code for name, code in results if name == "seal"]
    assert seal_results == [200]
    # 每个写入只有两种合法归宿：扫描前进闭包（200）或扫描后被拒（409）
    for name, code in results:
        if name != "seal":
            assert code in (200, 409)

    batches = client.get("/api/close/batches").json()
    sealed_batches = [b for b in batches if b["status"] == "sealed"]
    assert len(sealed_batches) == 1
    current = sealed_batches[0]
    assert current["closure_ok"] is True

    # 闭包自洽：现行批钉扎成员 == 库里该营业日全部流水
    conn = _raw_conn()
    try:
        from app.seal_window import parse_closure_ids

        day_count = conn.execute(
            "SELECT COUNT(*) c FROM cash_lines WHERE business_day=?", (DAY,)
        ).fetchone()["c"]
        pinned = parse_closure_ids(current["closure_line_ids"])
        assert len(pinned) == day_count
        assert current["day_line_count"] == day_count
    finally:
        conn.close()


def test_concurrent_double_seal_single_winner(client, monkeypatch):
    """两个封账在扫描门处交错：只有一个赢，另一个 409。

    第一封在进程门切定后、事务提交前被挂起，第二封此刻到达，
    确定性复现“并发双封”，不依赖调度运气。
    """
    import app.services.close_service as close_svc
    import app.seal_window as seal_window

    gate_closed = threading.Event()
    release = threading.Event()
    outcomes: list[int] = []
    first_holder: dict[bool] = {"held": False}
    real_begin = seal_window.begin_scan

    def tracked_begin(day):
        result = real_begin(day)  # 进程门此刻真正切定
        if not first_holder["held"]:
            # 只挂起第一封；第二封的 begin_scan 已在门里拿到 409，不会走到这之后
            first_holder["held"] = True
            gate_closed.set()
            release.wait(timeout=5)
        return result

    monkeypatch.setattr(close_svc.seal_window, "begin_scan", tracked_begin)

    def first_seal():
        outcomes.append(_seal(client).status_code)

    def second_seal():
        # TestClient 在线程内把服务端异常透传为原语异常而非 HTTP 响应；
        # 门控原语即第二封被拒的等价信号，过滤掉它的线程告警。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", pytest.PytestUnhandledThreadExceptionWarning)
            try:
                outcomes.append(_seal(client).status_code)
            except seal_window.SealScanConflict:
                outcomes.append(409)

    t1 = threading.Thread(target=first_seal)
    t1.start()
    assert gate_closed.wait(timeout=5)
    # 第一封进程门已切、事务未提交；第二封从独立线程到达必须 409
    t2 = threading.Thread(target=second_seal)
    t2.start()
    t2.join(timeout=10)
    release.set()
    t1.join(timeout=10)
    assert sorted(outcomes) == [200, 409]

    conn = _raw_conn()
    try:
        sealed = conn.execute(
            "SELECT COUNT(*) c FROM close_batches WHERE business_day=? AND status='sealed'",
            (DAY,),
        ).fetchone()["c"]
        assert sealed == 1
    finally:
        conn.close()


def test_double_seal_rejected_with_http_409_not_500(client, monkeypatch):
    """HTTP 层收口：并发双封的第二封必须是 409，门控原语不得泄漏成 500。"""
    import app.services.close_service as close_svc
    import app.seal_window as seal_window

    gate_closed = threading.Event()
    release = threading.Event()
    holder: dict[bool] = {"held": False}
    real_begin = seal_window.begin_scan

    def tracked_begin(day):
        result = real_begin(day)
        if not holder["held"]:
            holder["held"] = True
            gate_closed.set()
            release.wait(timeout=5)
        return result

    monkeypatch.setattr(close_svc.seal_window, "begin_scan", tracked_begin)

    http_codes: list[int] = []

    def first():
        http_codes.append(_seal(client).status_code)

    def second():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", pytest.PytestUnhandledThreadExceptionWarning)
            http_codes.append(_seal(client).status_code)

    t1 = threading.Thread(target=first)
    t1.start()
    assert gate_closed.wait(timeout=5)
    t2 = threading.Thread(target=second)
    t2.start()
    t2.join(timeout=10)
    release.set()
    t1.join(timeout=10)
    assert sorted(http_codes) == [200, 409]

def test_legacy_batch_gets_verifiable_closure(legacy_db):
    # 复用迁移夹具：旧库升级后批次必须带闭包成员与指纹且校验通过
    import app.seed as seed
    from app.db import connect
    from app.repositories import closes as close_repo

    seed.seed_if_empty()
    conn = connect()
    try:
        batch = close_repo.get_sealed_batch_for_day(conn, "2026-11-16")
        assert batch is not None
        assert batch["fingerprint"]
        assert batch["closure_line_ids"]
        verdict = close_repo.verify_batch_closure(conn, batch)
        assert verdict["ok"], verdict["problems"]

        state = conn.execute(
            "SELECT phase FROM seal_state WHERE business_day='2026-11-16'"
        ).fetchone()
        assert state is not None and state["phase"] == "sealed"
    finally:
        conn.close()


def test_orphan_scanning_state_is_cleared_on_restart(client):
    """封账进程在 scanning 态崩溃：重启重建清掉孤儿扫描门，营业日恢复可写可封。"""
    import app.seed as seed
    import app.seal_window as seal_window

    conn = _raw_conn()
    try:
        conn.execute(
            "INSERT INTO seal_state(business_day, phase, batch_id, updated_at) "
            "VALUES (?, 'scanning', NULL, 'x')",
            (DAY,),
        )
        conn.commit()
    finally:
        conn.close()
    # 模拟进程重启：进程内门也从零开始
    seal_window.reset_for_tests()

    # 重启：再次执行建库/迁移/重建
    seed.seed_if_empty()

    conn = _raw_conn()
    try:
        orphan = conn.execute(
            "SELECT COUNT(*) c FROM seal_state WHERE business_day=?", (DAY,)
        ).fetchone()["c"]
        assert orphan == 0
    finally:
        conn.close()

    # 营业日可正常写入与封账
    r = client.post(
        "/api/lines",
        json={"business_day": DAY, "till_code": "TILL-A", "amount": 3.0},
    )
    assert r.status_code == 200, r.text
    assert _seal(client).status_code == 200


@pytest.fixture()
def legacy_db(tmp_path, monkeypatch):
    import app.config as config
    import app.db as db

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / "dayseal.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE tills (id INTEGER PRIMARY KEY, till_code TEXT NOT NULL UNIQUE, title TEXT NOT NULL);
            CREATE TABLE cash_lines (
              id INTEGER PRIMARY KEY, line_no TEXT NOT NULL UNIQUE, till_code TEXT NOT NULL,
              business_day TEXT NOT NULL, occurred_at TEXT NOT NULL, amount REAL NOT NULL,
              pay_method TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE close_batches (
              id INTEGER PRIMARY KEY, batch_no TEXT NOT NULL UNIQUE, business_day TEXT NOT NULL,
              system_total REAL NOT NULL, declared_cash REAL NOT NULL, variance REAL NOT NULL,
              line_count INTEGER NOT NULL, status TEXT NOT NULL, sealed_at TEXT NOT NULL, note TEXT
            );
            CREATE TABLE variance_rows (
              id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, business_day TEXT NOT NULL,
              variance REAL NOT NULL, disposition TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('business_day','2026-11-16')"
        )
        conn.execute(
            "INSERT INTO tills(till_code,title) VALUES('TILL-A','一号')"
        )
        for no, amount, method in [
            ("CL-1", 100.0, "现金"),
            ("CL-2", 50.0, "微信"),
            ("CL-3", 25.0, "现金"),
        ]:
            conn.execute(
                "INSERT INTO cash_lines(line_no,till_code,business_day,occurred_at,amount,pay_method,created_at) "
                "VALUES(?, 'TILL-A', '2026-11-16', 'x', ?, ?, 'x')",
                (no, amount, method),
            )
        conn.execute(
            "INSERT INTO close_batches(batch_no,business_day,system_total,declared_cash,variance,"
            "line_count,status,sealed_at) VALUES('CB-0001','2026-11-16',175,170,-5,3,'sealed','x')"
        )
        conn.execute(
            "INSERT INTO variance_rows(batch_id,business_day,variance,disposition,created_at) "
            "VALUES(1,'2026-11-16',-5,'pending','x')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", path)
    return path
