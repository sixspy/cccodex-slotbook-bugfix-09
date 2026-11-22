"""档期盘：开档、关档、容量查询、仪表聚合（剔关盘）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from auth import AuthHeader, require_user, require_writer
from db import connect, write_audit
from locks import release, require_lock, try_acquire

router = APIRouter(tags=["slots"])

# 仪表剔关盘卡（2026-11-22 封卡，当日禁止改口；见 ops/仪表剔关盘卡.md）
# 甲路径：聚合时同事务排除已关盘并写回仪表数，失败则仪表数滚回旧值。
# 乙路径（剔关盘影子校准）禁用，两边不得混用。
DASHBOARD_CARD = {"sealed": "2026-11-22", "path": "甲", "exclude_closed": True}


def aggregate_dashboard(conn) -> dict:
    """甲路径聚合：同事务内剔除关盘档。"""
    return {
        "slots_open": conn.execute(
            "SELECT COUNT(*) c FROM slots WHERE status<>'关闭'"
        ).fetchone()["c"],
        "slots_closed": conn.execute(
            "SELECT COUNT(*) c FROM slots WHERE status='关闭'"
        ).fetchone()["c"],
        "holds_open": conn.execute(
            "SELECT COUNT(*) c FROM holds WHERE status='占位中'"
        ).fetchone()["c"],
        "bookings": conn.execute(
            "SELECT COUNT(*) c FROM bookings WHERE status='已确认'"
        ).fetchone()["c"],
        "waitlist": conn.execute(
            "SELECT COUNT(*) c FROM waitlist WHERE status='候补中'"
        ).fetchone()["c"],
    }


def write_dashboard_stats(conn, actor: str, metrics: dict, *, path: str, exclude_closed: bool) -> None:
    """仪表数唯一写入口。偏离仪表剔关盘卡：拒写并记偏，仪表数零改。"""
    if path != DASHBOARD_CARD["path"] or exclude_closed != DASHBOARD_CARD["exclude_closed"]:
        write_audit(
            conn,
            actor,
            "记偏",
            "仪表剔关盘卡",
            {"path": path, "exclude_closed": exclude_closed, "sealed": DASHBOARD_CARD},
        )
        conn.commit()  # 只落记偏，仪表数零写
        raise HTTPException(409, "偏离仪表剔关盘卡：拒写并记偏")
    now = datetime.now().isoformat(timespec="seconds")
    for k, v in metrics.items():
        conn.execute(
            """INSERT INTO dashboard_stats(metric,value,updated_at) VALUES (?,?,?)
               ON CONFLICT(metric) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (k, int(v), now),
        )


class SlotIn(BaseModel):
    code: str
    title: str
    start_at: str
    end_at: str
    capacity: int = Field(ge=1, le=500)


class CloseIn(BaseModel):
    code: str


@router.get("/api/slots")
def list_slots(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM slots ORDER BY start_at, code"
        ).fetchall()
        # 剔关盘：关盘档不可订（可占列表与仪表同态，仪表剔关盘卡）
        items = []
        for r in rows:
            d = dict(r)
            d["bookable"] = r["status"] == "开放"
            items.append(d)
        return {"items": items}


@router.get("/api/slots/{code}")
def get_slot(code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute("SELECT * FROM slots WHERE code=?", (code,)).fetchone()
        if not row:
            raise HTTPException(404, "档期不存在")
        return dict(row)


@router.post("/api/slots")
def create_slot(body: SlotIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        require_lock(conn, f"slot:{body.code}", user)
        try:
            exists = conn.execute(
                "SELECT id FROM slots WHERE code=?", (body.code,)
            ).fetchone()
            if exists:
                raise HTTPException(409, "档期编码已存在")
            if body.end_at <= body.start_at:
                raise HTTPException(400, "结束时间必须晚于开始")
            conn.execute(
                """INSERT INTO slots(code,title,start_at,end_at,capacity,booked,status,generation)
                   VALUES (?,?,?,?,?,0,'开放',1)""",
                (body.code, body.title, body.start_at, body.end_at, body.capacity),
            )
            write_audit(conn, user, "slot_create", body.code, body.model_dump())
            conn.commit()
        finally:
            release(conn, f"slot:{body.code}", user)
            conn.commit()
    return {"ok": True, "code": body.code}


@router.post("/api/slots/close")
def close_slot(body: CloseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        lock_key = f"slot:{body.code}"
        if not try_acquire(conn, lock_key, user):
            conn.commit()  # 后到只记争用，业务零写
            raise HTTPException(409, "争用：后到只记争用，不写业务")
        try:
            row = conn.execute(
                "SELECT * FROM slots WHERE code=?", (body.code,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "档期不存在")
            if row["status"] == "关闭":
                raise HTTPException(409, "档期已关闭")
            open_holds = conn.execute(
                "SELECT COUNT(*) c FROM holds WHERE slot_id=? AND status='占位中'",
                (row["id"],),
            ).fetchone()["c"]
            if open_holds:
                raise HTTPException(409, "仍有未确认占位，禁止关档")
            conn.execute(
                "UPDATE slots SET status='关闭' WHERE id=?", (row["id"],)
            )
            write_audit(conn, user, "slot_close", body.code, {"generation": row["generation"]})
            conn.commit()
        finally:
            release(conn, lock_key, user)
            conn.commit()
    return {"ok": True}


@router.get("/api/dashboard")
def dashboard(authorization: AuthHeader = None):
    """刷仪表（甲路径）：占档锁 → 同事务剔关盘聚合并写回仪表数。

    失败则仪表数滚回旧值；与关盘同占 slot:<code> 档锁，后到只记争用。
    """
    user = require_user(authorization)
    with connect() as conn:
        codes = [
            r["code"]
            for r in conn.execute("SELECT code FROM slots ORDER BY code").fetchall()
        ]
        acquired, contended = [], None
        for code in codes:
            if try_acquire(conn, f"slot:{code}", user):
                acquired.append(code)
            else:
                contended = code
                break
        if contended is not None:
            for code in acquired:
                release(conn, f"slot:{code}", user)
            write_audit(conn, user, "dashboard_contention", "dashboard", {"lock": f"slot:{contended}"})
            conn.commit()  # 后到只记争用，业务零写
            raise HTTPException(409, "争用：后到只记争用，不写业务")
        try:
            metrics = aggregate_dashboard(conn)
            write_dashboard_stats(
                conn,
                user,
                metrics,
                path=DASHBOARD_CARD["path"],
                exclude_closed=DASHBOARD_CARD["exclude_closed"],
            )
            write_audit(conn, user, "dashboard_refresh", "dashboard", {"path": "甲", **metrics})
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()  # 失败则仪表数滚回旧值
            raise HTTPException(500, "仪表聚合失败：仪表数已滚回旧值")
        finally:
            for code in acquired:
                release(conn, f"slot:{code}", user)
            conn.commit()
        return {
            "slots_open": metrics["slots_open"],
            "slots_bookable": metrics["slots_open"],  # 可订数：剔关盘
            "slots_closed": metrics["slots_closed"],
            "holds_open": metrics["holds_open"],
            "bookings": metrics["bookings"],
            "waitlist": metrics["waitlist"],
            "path": DASHBOARD_CARD["path"],
            "now": datetime.now().isoformat(timespec="seconds"),
        }
