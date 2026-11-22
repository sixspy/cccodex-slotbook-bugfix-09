"""仪表剔关盘：假绿夹具 → 失败测（红）→ 修复后同态验证（绿）。

口径封卡见 ops/仪表剔关盘卡.md（甲路径，2026-11-22 封卡，当日禁止改口）：
聚合时同事务排除已关盘并写回仪表数，失败则仪表数滚回旧值；乙路径禁用。
本机夹具与文件库：tmp_path 文件库 + fixtures 种子；登录与库路径不变。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import app  # noqa: E402
from db import init_db  # noqa: E402
import db as dbmod  # noqa: E402
import slots_svc  # noqa: E402

CLOSED = "S-EVE-03"  # 夹具关盘档（无占位中，可关）


def _login(c: TestClient, username: str) -> dict:
    r = c.post("/api/login", json={"username": username, "password": "123456"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def closed_still_counted_env(tmp_path, monkeypatch):
    """假绿夹具：关盘后仪表仍计入（缺陷现场复现）。

    关盘 S-EVE-03 后，缺陷系统的仪表可订数仍把它算进去（假绿）。
    """
    db = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    if db.exists():
        db.unlink()
    init_db()
    with TestClient(app) as c:
        admin = _login(c, "admin")
        clerk = _login(c, "clerk")
        r = c.post("/api/slots/close", headers=admin, json={"code": CLOSED})
        assert r.status_code == 200, r.text
        yield {"client": c, "admin": admin, "clerk": clerk, "closed": CLOSED}


def test_dashboard_excludes_closed(closed_still_counted_env):
    """红1：仪表含关盘 —— 关盘后仪表可订数必须剔除关盘档。"""
    env = closed_still_counted_env
    c, h = env["client"], env["admin"]
    # 仪表页：可订数剔关盘（3 档关 1 应剩 2）
    dash = c.get("/api/dashboard", headers=h).json()
    assert dash["slots_open"] == 2, f"仪表可订数含关盘: {dash}"
    assert dash["slots_bookable"] == 2
    assert dash["slots_closed"] == 1
    # 可占列表：关盘档不可订，开放档可订
    items = {i["code"]: i for i in c.get("/api/slots", headers=h).json()["items"]}
    assert items[CLOSED]["status"] == "关闭"
    assert items[CLOSED]["bookable"] is False
    assert items["S-AM-01"]["bookable"] is True
    assert items["S-PM-02"]["bookable"] is True
    # 对账页：汇总剔除关盘档
    recon = c.get("/api/reconcile", headers=h).json()
    assert recon["include_closed"] is False
    assert CLOSED not in [i["slot_code"] for i in recon["items"]]


def test_close_and_dashboard_refresh_contention(closed_still_counted_env):
    """红2：两人同时关盘与刷仪表须占档锁，后到只记争用。"""
    env = closed_still_counted_env
    c, admin, clerk = env["client"], env["admin"], env["clerk"]
    # 甲（admin）持档锁 = 关盘进行中；乙（clerk）刷仪表 → 409 只记争用
    r = c.post("/api/locks/acquire", headers=admin, json={"lock_key": "slot:S-PM-02"})
    assert r.status_code == 200, r.text
    r = c.get("/api/dashboard", headers=clerk)
    assert r.status_code == 409, f"刷仪表未占档锁: {r.status_code} {r.text}"
    cont = c.get("/api/contention", headers=admin).json()["items"]
    assert any(x["lock_key"] == "slot:S-PM-02" and x["actor"] == "clerk" for x in cont)
    audits = c.get("/api/audit", headers=admin).json()["items"]
    assert not any(a["actor"] == "clerk" and a["action"] == "dashboard_refresh" for a in audits)
    # 业务零写：乙争用后仪表数零改（此前无人成功刷表，仪表数应为空）
    with dbmod.connect() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM dashboard_stats").fetchone()["c"]
    assert n == 0
    # 反向：乙持档锁，甲关盘 → 409 争用落库，关盘不生效
    c.post("/api/locks/release", headers=admin, json={"lock_key": "slot:S-PM-02"})
    r = c.post("/api/locks/acquire", headers=clerk, json={"lock_key": "slot:S-AM-01"})
    assert r.status_code == 200, r.text
    r = c.post("/api/slots/close", headers=admin, json={"code": "S-AM-01"})
    assert r.status_code == 409, f"关盘未守档锁: {r.status_code} {r.text}"
    cont = c.get("/api/contention", headers=admin).json()["items"]
    assert any(x["lock_key"] == "slot:S-AM-01" and x["actor"] == "admin" for x in cont)
    slot = c.get("/api/slots/S-AM-01", headers=admin).json()
    assert slot["status"] == "开放"


def test_hold_contention_recorded(closed_still_counted_env):
    """占位服务同守档锁纪律：后到只记争用，业务零写。"""
    env = closed_still_counted_env
    c, admin, clerk = env["client"], env["admin"], env["clerk"]
    r = c.post("/api/locks/acquire", headers=admin, json={"lock_key": "slot:S-AM-01"})
    assert r.status_code == 200, r.text
    r = c.post(
        "/api/holds",
        headers=clerk,
        json={"slot_code": "S-AM-01", "party_key": "P-CONT", "seats": 1},
    )
    assert r.status_code == 409, r.text
    cont = c.get("/api/contention", headers=admin).json()["items"]
    assert any(x["lock_key"] == "slot:S-AM-01" and x["actor"] == "clerk" for x in cont)
    holds = c.get("/api/holds", headers=admin).json()["items"]
    assert not any(x["party_key"] == "P-CONT" for x in holds)


def test_hold_on_closed_rejected(closed_still_counted_env):
    """占位服务同态：关盘档禁止占位。"""
    env = closed_still_counted_env
    r = env["client"].post(
        "/api/holds",
        headers=env["admin"],
        json={"slot_code": CLOSED, "party_key": "P-Z", "seats": 1},
    )
    assert r.status_code == 409, r.text


def test_five_surfaces_consistent(closed_still_counted_env):
    """仪表页、关盘态、可占列表、对账页、审计页五处同态。"""
    env = closed_still_counted_env
    c, h = env["client"], env["admin"]
    # 仪表页：可订数剔关盘
    dash = c.get("/api/dashboard", headers=h).json()
    assert dash["slots_bookable"] == 2 and dash["slots_closed"] == 1
    # 关盘态：档态=关闭
    assert c.get(f"/api/slots/{CLOSED}", headers=h).json()["status"] == "关闭"
    # 可占列表：关盘档 bookable=false
    items = {i["code"]: i for i in c.get("/api/slots", headers=h).json()["items"]}
    assert items[CLOSED]["bookable"] is False
    # 对账页：汇总剔关盘
    recon = c.get("/api/reconcile", headers=h).json()
    assert recon["include_closed"] is False
    assert CLOSED not in [i["slot_code"] for i in recon["items"]]
    # 审计页：关盘与刷仪表同态留痕
    audits = c.get("/api/audit", headers=h).json()["items"]
    assert any(a["action"] == "slot_close" and a["target"] == CLOSED for a in audits)
    assert any(a["action"] == "dashboard_refresh" for a in audits)


def test_deviation_refused_and_recorded(closed_still_counted_env):
    """偏离仪表剔关盘卡（乙路径混用 / 未剔关盘）：拒写并记偏，仪表数零改。"""
    with dbmod.connect() as conn:
        for bad in (
            {"path": "乙", "exclude_closed": True},
            {"path": "甲", "exclude_closed": False},
        ):
            with pytest.raises(HTTPException) as ei:
                slots_svc.write_dashboard_stats(conn, "tester", {"slots_open": 9}, **bad)
            assert ei.value.status_code == 409
        rows = conn.execute(
            "SELECT * FROM audit_trail WHERE action='记偏' AND target='仪表剔关盘卡'"
        ).fetchall()
        assert len(rows) == 2
        n = conn.execute("SELECT COUNT(*) c FROM dashboard_stats").fetchone()["c"]
        assert n == 0, "偏离卡仍写了仪表数"


def test_dashboard_failure_rolls_back(closed_still_counted_env, monkeypatch):
    """甲路径：聚合写回失败，仪表数滚回旧值。"""
    env = closed_still_counted_env
    c, h = env["client"], env["admin"]
    dash = c.get("/api/dashboard", headers=h).json()
    assert dash["slots_open"] == 2

    def boom(*a, **k):
        raise RuntimeError("模拟聚合写回故障")

    monkeypatch.setattr(slots_svc, "write_dashboard_stats", boom)
    r = c.get("/api/dashboard", headers=h)
    assert r.status_code == 500, r.text
    with dbmod.connect() as conn:
        row = conn.execute(
            "SELECT value FROM dashboard_stats WHERE metric='slots_open'"
        ).fetchone()
    assert row["value"] == 2, "失败后仪表数未滚回旧值"
