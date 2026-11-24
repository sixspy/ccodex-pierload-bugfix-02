"""闸口放行：须无开放超重。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, require_lock
from models import GateReleaseIn
from validators import (
    open_overload_holds,
    validate_no_open_overload,
    validate_ticket_mutable,
)

router = APIRouter(tags=["gate"])


@router.get("/api/gate/releases")
def list_releases(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT g.*, t.ticket_code, t.vehicle_plate, l.lane_code
               FROM gate_releases g
               JOIN weigh_tickets t ON t.id=g.ticket_id
               JOIN weigh_lanes l ON l.id=g.lane_id
               ORDER BY g.id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/gate/release")
def release_gate(body: GateReleaseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    gate_key = f"gate:{body.ticket_id}"
    # 闸口锁管两人同票放行；票锁管放行与超重开挂/闭环的先后，防止两账交叉
    lock_keys = (gate_key, f"ticket:{body.ticket_id}")
    acquired: list[str] = []
    with connect() as conn:
        try:
            for key in lock_keys:
                require_lock(conn, key, user)
                acquired.append(key)
            ticket = conn.execute(
                """SELECT t.*, l.id lane_id FROM weigh_tickets t
                   JOIN weigh_lanes l ON l.id=t.lane_id WHERE t.id=?""",
                (body.ticket_id,),
            ).fetchone()
            if not ticket:
                raise HTTPException(404, "过磅票不存在")
            validate_ticket_mutable(dict(ticket))
            if ticket["status"] not in ("已裁定", "旁路挂起"):
                raise HTTPException(409, "票态不允许放行")
            if ticket["status"] == "旁路挂起":
                raise HTTPException(409, "旁路挂起须先闭环")
            # 超重未清禁放：以超重账开放挂起为准，不看票态单边
            validate_no_open_overload(conn, body.ticket_id)
            exists = conn.execute(
                "SELECT id FROM gate_releases WHERE ticket_id=? AND status='已放行'",
                (body.ticket_id,),
            ).fetchone()
            if exists:
                raise HTTPException(409, "已放行")
            now = now_iso()
            ref = f"GR-{ticket['ticket_code']}-{now[:10]}"
            cur = conn.execute(
                """INSERT INTO gate_releases(ticket_id,lane_id,release_ref,status,officer,released_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    body.ticket_id,
                    ticket["lane_id"],
                    ref,
                    "已放行",
                    body.officer,
                    now,
                ),
            )
            conn.execute(
                "UPDATE weigh_tickets SET status='已放行' WHERE id=?",
                (body.ticket_id,),
            )
            write_audit(
                conn,
                user,
                "gate_release",
                ref,
                {"release_id": cur.lastrowid, "ticket_id": body.ticket_id},
            )
            # 放行校验与写入须同一账页：超重未清整笔回滚，正式册零增
            still_open = open_overload_holds(conn, body.ticket_id)
            if still_open:
                conn.rollback()
                raise HTTPException(409, "超重未清禁放：放行整笔回滚")
            conn.commit()
            return {
                "id": cur.lastrowid,
                "release_ref": ref,
                "status": "已放行",
            }
        finally:
            for key in reversed(acquired):
                release(conn, key, user)