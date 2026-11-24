"""BUGFIX-17 禁放修复：开放超重挂起未闭环，闸口放行整笔回滚，正式册零增。

口径：超重未清禁放（以超重账开放挂起为准，不以票面单边为准），采用甲路径。

先红（改实现前用测试器跑）：
  A. 开放挂起仍在时放行，正式放行册必须零增（改前会增）。
  B. 未晋升放行意图不得被对账/证明当已放行（改前会当已放行）。
  C. 两人同时抢同一票放行须抢闸口锁，败方只记争用痕（改前无争用痕）。
  D. 总览/放行/超重/过磅/对账五页须同态（改前各页口径不一）。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

import db
import gate_svc
from app import app


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    db.init_db()
    yield


def _auth(c: TestClient, user="admin", password="admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _open_hold_on_adjudicated(ticket_code: str) -> int:
    """夹具：已裁定票上存在开放超重挂起（历史/旁路混入的存量态）。"""
    with db.connect() as conn:
        t = conn.execute(
            "SELECT id, gross_t FROM weigh_tickets WHERE ticket_code=?",
            (ticket_code,),
        ).fetchone()
        assert t is not None
        conn.execute(
            """INSERT INTO overload_holds(ticket_id,hold_key,overweight_t,threshold_t,
               status,opened_at,side_lane_note) VALUES (?,?,?,?,?,?,?)""",
            (
                t["id"],
                f"HOLD-FIX17-{ticket_code}",
                0.5,
                float(t["gross_t"]),
                "开放",
                db.now_iso(),
                "存量开放挂起",
            ),
        )
        conn.commit()
    return int(t["id"])


def _release_count(ticket_id: int) -> int:
    with db.connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM gate_releases WHERE ticket_id=?",
            (ticket_id,),
        ).fetchone()["c"]


def test_a_open_hold_release_rolls_back_formal_register_zero():
    """甲路径：超重未清则放行整笔回滚，正式放行册零增。"""
    c = TestClient(app)
    h = _auth(c)
    tid = _open_hold_on_adjudicated("WT-LN-N-01-IN-001")
    before = _release_count(tid)

    r = c.post(
        "/api/gate/release", headers=h, json={"ticket_id": tid, "officer": "抢放"}
    )

    assert r.status_code == 409, r.text
    assert _release_count(tid) == before

    detail = c.get(f"/api/tickets/{tid}", headers=h).json()
    # 票面存量仍为已裁定，但五页口径统一显示“超重未清”，不得显示可放行
    assert detail["ticket"]["stored_status"] == "已裁定"
    assert detail["ticket"]["status"] == "超重未清"
    assert detail["ticket"]["release_blocked"] is True
    assert detail["gate_releases"] == []
    assert any(
        x["status"] == "开放" for x in detail["overload_holds"]
    )


def test_b_unpromoted_release_not_released_by_reconcile():
    """乙路径禁混用：未晋升放行不得被对账/证明当成已放行。"""
    c = TestClient(app)
    h = _auth(c)
    tid = _open_hold_on_adjudicated("WT-LN-N-01-IN-001")

    c.post("/api/gate/release", headers=h, json={"ticket_id": tid})

    proof = c.get("/api/proof/ticket/WT-LN-N-01-IN-001", headers=h).json()
    assert proof["released"] is False
    assert proof["overload_closed"] is False
    assert proof["coherent"] is False

    rec = c.get("/api/reconcile/BL-2026-0101", headers=h).json()
    entry = next(
        t for t in rec["tickets"] if t["ticket_code"] == "WT-LN-N-01-IN-001"
    )
    assert entry["status"] != "已放行"

    detail = c.get(f"/api/tickets/{tid}", headers=h).json()
    assert detail["gate_releases"] == []


def test_c_concurrent_same_ticket_release_lock_and_contention(monkeypatch):
    """争用：两路会话并发打同票放行，败方只记争用痕，正式册只增一行。"""
    c_admin = TestClient(app)
    c_writer = TestClient(app)
    h_admin = _auth(c_admin, "admin", "admin123")
    h_writer = _auth(c_writer, "writer", "writer123")

    lanes = c_admin.get("/api/lanes", headers=h_admin).json()["items"]
    lane = next(x for x in lanes if x["lane_code"] == "LN-S-01-OUT")
    ticket = c_admin.post(
        "/api/tickets",
        headers=h_admin,
        json={
            "lane_id": lane["id"],
            "bl_ref": "BL-2026-0102",
            "vehicle_plate": "测C·00003",
            "axle_weights": [{"axle": 1, "w": 6.0}, {"axle": 2, "w": 5.5}],
            "ticket_code": "WT-FIX17-RACE",
        },
    ).json()
    assert ticket["status"] == "已裁定"

    winner_in_business = threading.Event()
    loser_tried = threading.Event()
    real_write_audit = gate_svc.write_audit

    def blocked_audit(conn, actor, action, target, payload):
        if action == "gate_release" and not winner_in_business.is_set():
            winner_in_business.set()
            loser_tried.wait(timeout=5)
        return real_write_audit(conn, actor, action, target, payload)

    monkeypatch.setattr(gate_svc, "write_audit", blocked_audit)

    results: dict[str, object] = {}

    def winner():
        results["admin"] = c_admin.post(
            "/api/gate/release",
            headers=h_admin,
            json={"ticket_id": ticket["id"], "officer": "闸口甲"},
        )

    def loser():
        winner_in_business.wait(timeout=10)
        results["writer"] = c_writer.post(
            "/api/gate/release",
            headers=h_writer,
            json={"ticket_id": ticket["id"], "officer": "闸口乙"},
        )
        loser_tried.set()

    t1 = threading.Thread(target=winner)
    t2 = threading.Thread(target=loser)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert results["admin"].status_code == 200, results["admin"].text
    assert results["writer"].status_code == 409, results["writer"].text
    assert _release_count(ticket["id"]) == 1

    cont = c_admin.get("/api/contention", headers=h_admin).json()["items"]
    assert any(
        x["lock_key"] == f"gate:{ticket['id']}" and x["actor"] == "writer"
        for x in cont
    )


def test_d_five_pages_same_state():
    """五页同态：总览/放行/超重/过磅/对账对同一混用态给出同一禁放口径。"""
    c = TestClient(app)
    h = _auth(c)
    tid = _open_hold_on_adjudicated("WT-LN-N-01-IN-001")
    code = "WT-LN-N-01-IN-001"

    tickets = c.get("/api/tickets", headers=h).json()["items"]
    board = next(x for x in tickets if x["ticket_code"] == code)
    assert board["status"] == "超重未清"
    assert board["release_blocked"] is True

    holds = c.get("/api/overloads", headers=h).json()["items"]
    hold = next(x for x in holds if x["hold_key"] == f"HOLD-FIX17-{code}")
    assert hold["ticket_status"] == "超重未清"
    assert hold["release_blocked"] is True

    dash = c.get("/api/dashboard", headers=h).json()
    assert dash["release_blocked"] >= 1
    assert dash["releasable"] == 0

    rec = c.get("/api/reconcile/BL-2026-0101", headers=h).json()
    entry = next(t for t in rec["tickets"] if t["ticket_code"] == code)
    assert entry["status"] == "超重未清"
    assert entry["release_blocked"] is True
    assert rec["release_blocked_count"] >= 1

    detail = c.get(f"/api/tickets/{tid}", headers=h).json()
    assert detail["ticket"]["status"] == "超重未清"

    gate = c.post("/api/gate/release", headers=h, json={"ticket_id": tid})
    assert gate.status_code == 409
    assert _release_count(tid) == 0
