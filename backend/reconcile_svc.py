"""提单册与过磅票对账证明。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from validators import validate_bl_match

router = APIRouter(tags=["reconcile"])


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    from db import connect

    with connect() as conn:
        rows = conn.execute(
            """SELECT bl_code, declared_gross_t FROM bl_manifests ORDER BY bl_code"""
        ).fetchall()
        items = []
        for bl in rows:
            items.append(_reconcile_bl(conn, bl["bl_code"]))
        return {"items": items}


@router.get("/api/reconcile/{bl_code}")
def reconcile_one(bl_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    from db import connect

    with connect() as conn:
        bl = conn.execute(
            "SELECT * FROM bl_manifests WHERE bl_code=?", (bl_code,)
        ).fetchone()
        if not bl:
            raise HTTPException(404, "提单不存在")
        return _reconcile_bl(conn, bl_code)


def _reconcile_bl(conn, bl_code: str) -> dict:
    from db import ticket_release_flags, effective_ticket_status

    bl = conn.execute(
        "SELECT * FROM bl_manifests WHERE bl_code=?", (bl_code,)
    ).fetchone()
    ticket_rows = conn.execute(
        """SELECT id, ticket_code, gross_t, status, generation FROM weigh_tickets
           WHERE bl_ref=? AND status NOT IN ('作废')""",
        (bl_code,),
    ).fetchall()
    if not ticket_rows:
        return {
            "bl_code": bl_code,
            "declared_gross_t": bl["declared_gross_t"],
            "ticket_sum_t": 0,
            "balanced": False,
            "reason": "无过磅票",
            "tickets": [],
        }
    ticket_sum = round(sum(float(t["gross_t"]) for t in ticket_rows), 3)
    declared = float(bl["declared_gross_t"])
    matched = validate_bl_match(declared, ticket_sum, tolerance=0.1)
    seal_rows = conn.execute(
        """SELECT s.seal_ref, s.ticket_generation, t.ticket_code
           FROM gate_seals s JOIN weigh_tickets t ON t.id=s.ticket_id
           WHERE t.bl_ref=?""",
        (bl_code,),
    ).fetchall()
    proof_ok = matched and len(seal_rows) >= 1
    tickets = []
    blocked = 0
    for t in ticket_rows:
        flags = ticket_release_flags(conn, int(t["id"]))
        status = effective_ticket_status(
            t["status"], flags["open_holds"], flags["released"]
        )
        if flags["open_holds"] and not flags["released"]:
            blocked += 1
        tickets.append(
            {
                "ticket_code": t["ticket_code"],
                "gross_t": t["gross_t"],
                "status": status,
                "stored_status": t["status"],
                "generation": t["generation"],
                "open_holds": flags["open_holds"],
                "released": flags["released"],
                "release_blocked": bool(
                    flags["open_holds"] and not flags["released"]
                ),
            }
        )
    return {
        "bl_code": bl_code,
        "declared_gross_t": declared,
        "ticket_sum_t": ticket_sum,
        "balanced": matched,
        "proof_ready": proof_ok,
        "seal_count": len(seal_rows),
        "release_blocked_count": blocked,
        "tickets": tickets,
        "seals": [dict(s) for s in seal_rows],
    }


@router.get("/api/proof/ticket/{ticket_code}")
def proof_ticket(ticket_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    from db import connect

    with connect() as conn:
        ticket = conn.execute(
            "SELECT * FROM weigh_tickets WHERE ticket_code=?", (ticket_code,)
        ).fetchone()
        if not ticket:
            raise HTTPException(404, "票不存在")
        bl = conn.execute(
            "SELECT * FROM bl_manifests WHERE bl_code=?", (ticket["bl_ref"],)
        ).fetchone()
        seal = conn.execute(
            """SELECT * FROM gate_seals WHERE ticket_id=? ORDER BY id DESC LIMIT 1""",
            (ticket["id"],),
        ).fetchone()
        release = conn.execute(
            """SELECT * FROM gate_releases WHERE ticket_id=? AND status='已放行'""",
            (ticket["id"],),
        ).fetchone()
        overload_closed = not conn.execute(
            """SELECT id FROM overload_holds WHERE ticket_id=? AND status='开放'""",
            (ticket["id"],),
        ).fetchone()
        manifest_match = False
        if seal and bl:
            manifest_match = validate_bl_match(
                float(bl["declared_gross_t"]), float(ticket["gross_t"])
            )
        return {
            "ticket_code": ticket_code,
            "bl_ref": ticket["bl_ref"],
            "gross_t": ticket["gross_t"],
            "manifest_matches_bl": manifest_match,
            "has_seal": seal is not None,
            "seal_generation": seal["ticket_generation"] if seal else None,
            "released": release is not None,
            "overload_closed": overload_closed,
            "coherent": bool(
                manifest_match and overload_closed and (seal or release)
            ),
        }