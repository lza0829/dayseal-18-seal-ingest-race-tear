"""封账口径同源性 / 快照可回溯 / 封后锁定 的端到端校验。

用同一组流水分别按「仅现金」与「全支付方式」封账两次，
核对系统合计与长短款不同、批次口径字段可回溯、封后改流水被拒。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app.config as config
    import app.seed as seed
    import app.db as db

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "dayseal.db")
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "dayseal.db")

    seed.seed_if_empty()

    from app.main import app as fastapi_app

    return TestClient(fastapi_app)


DAY = "2026-11-16"


def _set_scope(client, scope):
    r = client.put("/api/settings/close-scope", json={"scope": scope})
    assert r.status_code == 200, r.text
    return r.json()


def _seed_lines_for_today(client):
    # 种子数据 2026-11-16：现金 35.5 + 66 + 49.9 = 151.4（3 笔）
    # 微信 128 + 支付宝 210 = 338（2 笔）
    return client.get(f"/api/lines?business_day={DAY}").json()


def test_default_scope_is_cash_only(client):
    body = client.get("/api/settings/close-scope").json()
    assert body["scope"] == "cash_only"
    assert [o["value"] for o in body["options"]] == ["cash_only", "all_methods"]


def test_same_lines_two_scopes_produce_different_totals(client):
    lines = _seed_lines_for_today(client)
    assert len(lines) == 5

    # —— 仅现金 ——
    _set_scope(client, "cash_only")
    preview_cash = client.get(f"/api/close/preview?business_day={DAY}").json()
    assert preview_cash["scope"] == "cash_only"
    assert preview_cash["system_total"] == pytest.approx(151.4)
    assert preview_cash["line_count"] == 3
    assert preview_cash["cash_line_count"] == 3
    assert preview_cash["day_line_count"] == 5
    # 非现金仍在可查流水中，只是不计入
    assert len(preview_cash["lines"]) == 5
    included = {m["pay_method"]: m["included"] for m in preview_cash["methods"]}
    assert included == {"支付宝": False, "微信": False, "现金": True}

    # 实盘 155 → 长短款 3.6
    sealed_cash = client.post(
        "/api/close/seal", json={"business_day": DAY, "declared_cash": 155.0}
    )
    assert sealed_cash.status_code == 200, sealed_cash.text
    cash_batch = sealed_cash.json()
    assert cash_batch["scope"] == "cash_only"
    assert cash_batch["system_total"] == pytest.approx(151.4)
    assert cash_batch["variance"] == pytest.approx(3.6)
    assert cash_batch["cash_line_count"] == 3

    # 非现金流水没有被删掉
    assert len(client.get(f"/api/lines?business_day={DAY}").json()) == 5

    # —— 全支付方式 ——（换一天避免重复封账限制）
    day2 = "2026-11-17"
    _set_scope(client, "all_methods")
    preview_all = client.get(f"/api/close/preview?business_day={day2}").json()
    assert preview_all["scope"] == "all_methods"
    # 复制同一组流水到 day2
    for line in lines:
        r = client.post(
            "/api/lines",
            json={
                "business_day": day2,
                "till_code": line["till_code"],
                "amount": line["amount"],
                "pay_method": line["pay_method"],
            },
        )
        assert r.status_code == 200, r.text

    preview_all = client.get(f"/api/close/preview?business_day={day2}").json()
    assert preview_all["system_total"] == pytest.approx(489.4)
    assert preview_all["line_count"] == 5

    sealed_all = client.post(
        "/api/close/seal", json={"business_day": day2, "declared_cash": 155.0}
    )
    assert sealed_all.status_code == 200, sealed_all.text
    all_batch = sealed_all.json()
    assert all_batch["scope"] == "all_methods"
    assert all_batch["system_total"] == pytest.approx(489.4)
    assert all_batch["variance"] == pytest.approx(155.0 - 489.4)

    # 两口径数字确实不同
    assert cash_batch["system_total"] != all_batch["system_total"]
    assert cash_batch["variance"] != all_batch["variance"]


def test_batch_snapshots_traceable_after_scope_switch(client):
    _set_scope(client, "cash_only")
    assert client.post(
        "/api/close/seal", json={"business_day": DAY, "declared_cash": 151.4}
    ).status_code == 200

    batch = client.get("/api/close/batches").json()[0]
    assert batch["scope"] == "cash_only"
    assert batch["cash_line_count"] == 3
    assert batch["cash_total"] == pytest.approx(151.4)
    methods = {m["pay_method"]: m for m in batch["methods"]}
    assert set(methods) == {"现金", "微信", "支付宝"}
    assert methods["现金"]["included"] is True
    assert methods["微信"]["included"] is False

    # 切换口径不得改写已封批次
    before = client.get(f"/api/close/batches/{batch['id']}").json()
    _set_scope(client, "all_methods")
    after = client.get(f"/api/close/batches/{batch['id']}").json()
    for key in ("scope", "system_total", "cash_total", "cash_line_count", "variance",
                "declared_cash", "line_count", "day_line_count"):
        assert before[key] == after[key]
    assert before["methods"] == after["methods"]


def test_sealed_day_rejects_line_mutation(client):
    _set_scope(client, "cash_only")
    assert client.post(
        "/api/close/seal", json={"business_day": DAY, "declared_cash": 151.4}
    ).status_code == 200

    lines = client.get(f"/api/lines?business_day={DAY}").json()

    # 新增
    r = client.post(
        "/api/lines", json={"business_day": DAY, "till_code": "TILL-A", "amount": 9.9}
    )
    assert r.status_code == 409

    # 修改（现金 → 非现金也算改写有效流水）
    for line in lines:
        r = client.patch(f"/api/lines/{line['id']}", json={"amount": 1.0})
        assert r.status_code == 409

    # 删除
    for line in lines:
        r = client.delete(f"/api/lines/{line['id']}")
        assert r.status_code == 409

    # 数据原封不动
    remaining = client.get(f"/api/lines?business_day={DAY}").json()
    assert len(remaining) == 5


def test_sealed_day_rejects_mutation_at_db_layer(client):
    _set_scope(client, "cash_only")
    assert client.post(
        "/api/close/seal", json={"business_day": DAY, "declared_cash": 151.4}
    ).status_code == 200

    import sqlite3
    from app.config import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cash_lines(line_no, till_code, business_day, occurred_at, "
                "amount, pay_method, created_at) VALUES ('X','TILL-A',?, 'x', 1, '现金','x')",
                (DAY,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE close_batches SET system_total=0 WHERE business_day=?", (DAY,))
    finally:
        conn.close()


def test_unknown_scope_rejected(client):
    r = client.put("/api/settings/close-scope", json={"scope": "mixed"})
    assert r.status_code == 400


def test_dashboard_uses_same_scope(client):
    _set_scope(client, "cash_only")
    dash = client.get("/api/dashboard").json()
    assert dash["scope"] == "cash_only"
    assert dash["system_total_today"] == pytest.approx(151.4)
    assert dash["cash_total_today"] == pytest.approx(151.4)

    _set_scope(client, "all_methods")
    dash = client.get("/api/dashboard").json()
    assert dash["system_total_today"] == pytest.approx(489.4)
    assert dash["cash_total_today"] == pytest.approx(151.4)
