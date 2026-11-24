"""急停中断残额口径（方案 B：保留可结算残额）的夹具与回归。

覆盖：
1. 中断回执 / 会话状态 / 脉冲与金额 / 桩位枪线 / 事件 / 详情 / 中断页 / 总览 同刷；
2. 中断后可结算残额保留，且只允许收口一次；
3. 中断结局可重放（回执两次一致）；
4. 中断与脉冲对撞：中断后记脉冲必须被拒，落账不漂移；
5. 中断后仍占枪 / 残额静默清零 → 对账清单能暴露；
6. 禁止半截双态（已中断 + 已占枪 / 已中断 + 已关账并存）。
"""
from __future__ import annotations

import threading

from app.db import db


def _get(client, sid: int) -> dict:
    r = client.get(f"/api/会话/{sid}")
    assert r.status_code == 200, r.text
    return r.json()


def _pulse(client, sid: int, kwh: float = 1.5, kw: float = 7):
    return client.post(f"/api/会话/{sid}/脉冲", json={"电量": kwh, "功率": kw})


def _abort(client, sid: int, reason: str = "急停"):
    return client.post(f"/api/会话/{sid}/中断", json={"原因": reason})


def _settle(client, sid: int):
    return client.post(f"/api/会话/{sid}/结算", json={})


def _overview(client) -> dict:
    r = client.get("/api/总览")
    assert r.status_code == 200, r.text
    return r.json()


def _audit(client) -> dict:
    r = client.get("/api/中断对账")
    assert r.status_code == 200, r.text
    return r.json()


def _pile(client, pile_id: int) -> dict:
    for p in client.get("/api/桩位").json():
        if p["id"] == pile_id:
            return p
    raise AssertionError("桩位不存在")


def _gun(client, gun_id: int) -> dict:
    for g in client.get("/api/枪线").json():
        if g["id"] == gun_id:
            return g
    raise AssertionError("枪线不存在")


# --------------------------------------------------------------------------
# 1. 中断全链路同刷 + 残额保留
# --------------------------------------------------------------------------
def test_abort_freezes_residual_and_releases_everywhere(client, open_session):
    s = open_session(1)
    sid = s["会话编号"]
    assert _pulse(client, sid, 2.0).status_code == 200
    assert _pulse(client, sid, 1.5).status_code == 200

    before = _get(client, sid)
    expected_kwh = round(before["kwh"], 3)
    expected_amount = round(before["amount"], 2)
    assert expected_kwh == 3.5
    assert expected_amount > 0

    r = _abort(client, sid)
    assert r.status_code == 200, r.text
    rec = r.json()

    # 回执口径
    assert rec["状态"] == "已中断"
    assert rec["残额"]["电量"] == expected_kwh
    assert rec["残额"]["金额"] == expected_amount
    assert rec["脉冲"]["条数"] == 2
    assert rec["脉冲"]["电量和"] == expected_kwh
    assert rec["占用"]["占用已释放"] is True
    assert rec["结算"]["待收口"] is True
    assert rec["对账"]["残额可复算"] is True
    assert rec["对账"]["脉冲电量和相符"] is True

    # 会话状态 / 金额 / 脉冲
    d = _get(client, sid)
    assert d["status"] == "已中断"
    assert d["kwh"] == expected_kwh
    assert d["amount"] == expected_amount  # 残额不得被清零
    assert len(d["脉冲"]) == 2
    assert d["脉冲电量和"] == expected_kwh

    # 桩位与枪线已释放
    assert _pile(client, 1)["status"] == "空闲"
    assert _gun(client, s and d["gun_id"])["status"] == "可用"

    # 事件具名可追
    actions = [e["action"] for e in d["事件"]]
    assert "异常中断" in actions

    # 详情与回执同源
    assert d["中断"]["残额"]["金额"] == expected_amount
    assert d["中断"]["待收口"] is True

    # 总览已中断桶与金额同口径
    o = _overview(client)
    assert o["已中断"] == 1
    assert o["已中断待收口"] == 1
    assert o["已中断残额电量"] == expected_kwh
    assert o["已中断残额金额"] == expected_amount
    assert o["桩位占用"] == 0
    assert o["在充电量"] == 0

    # 对账干净
    assert _audit(client)["合计问题"] == 0


# --------------------------------------------------------------------------
# 2. 已中断再走一次收口：允许且只允许一次
# --------------------------------------------------------------------------
def test_aborted_session_settles_once(client, open_session):
    sid = open_session(1)["会话编号"]
    _pulse(client, sid, 2.0)
    rec = _abort(client, sid).json()
    frozen = rec["残额"]["金额"]
    kwh = rec["残额"]["电量"]

    r = _settle(client, sid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["状态"] == "已关账"
    assert body["来源"] == "中断收口"
    assert body["电量"] == kwh
    assert body["金额"] == frozen  # 收口金额必须等于冻结残额，不得重算变形

    d = _get(client, sid)
    assert d["status"] == "已关账"
    assert d["settled_at"] is not None
    assert d["中断"]["待收口"] is False
    assert any(e["action"] == "中断结算收口" for e in d["事件"])

    # 再次收口 / 再次中断都必须被拒
    assert _settle(client, sid).status_code in (400, 409)
    assert _abort(client, sid).status_code in (400, 409)

    # 总览已中断桶归零，关账金额只计一次
    o = _overview(client)
    assert o["已中断"] == 0
    assert o["已中断待收口"] == 0
    assert o["本日关账金额"] == frozen


# --------------------------------------------------------------------------
# 3. 中断结局可重放
# --------------------------------------------------------------------------
def test_abort_receipt_replayable(client, open_session):
    sid = open_session(1)["会话编号"]
    _pulse(client, sid, 1.0)
    first = _abort(client, sid).json()

    # POST 幂等重入不重复落事件、不改冻结点
    second = _abort(client, sid).json()
    assert second == first
    assert len(_get(client, sid)["事件"]) == len(first["事件"])

    # GET 回执独立重放，逐字段一致
    fetched = client.get(f"/api/会话/{sid}/中断回执")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == first


# --------------------------------------------------------------------------
# 4. 中断与脉冲对撞：中断后脉冲被拒，落账不漂移
# --------------------------------------------------------------------------
def test_pulse_after_abort_rejected_and_no_drift(client, open_session):
    sid = open_session(1)["会话编号"]
    _pulse(client, sid, 2.0)
    frozen = _abort(client, sid).json()["残额"]["金额"]

    r = _pulse(client, sid, 9.9)
    assert r.status_code == 400
    assert "已中断" in r.json()["detail"]

    d = _get(client, sid)
    assert d["amount"] == frozen
    assert d["kwh"] == 2.0
    assert len(d["脉冲"]) == 1
    assert _audit(client)["脉冲与电量漂移"] == []


def test_concurrent_abort_and_pulse_stay_consistent(client, open_session, db_path):
    """并发对撞：中断与脉冲只有一个能改状态，回执与落账必须一致。"""
    sid = open_session(1)["会话编号"]
    _pulse(client, sid, 1.0)

    results: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def do_abort():
        barrier.wait()
        results["abort"] = _abort(client, sid).status_code

    def do_pulse():
        barrier.wait()
        results["pulse"] = _pulse(client, sid, 5.0).status_code

    t1 = threading.Thread(target=do_abort)
    t2 = threading.Thread(target=do_pulse)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    d = _get(client, sid)
    rec = client.get(f"/api/会话/{sid}/中断回执")
    assert rec.status_code == 200
    rec = rec.json()

    # 两种合法交错都应收敛到同一结局：已中断，且残额 = 脉冲累计，可复算。
    assert results["abort"] == 200
    assert d["status"] == "已中断"
    if results["pulse"] == 400:
        # 中断先落账：脉冲必须被拒，残额停在中断前的 1.0kWh。
        assert d["kwh"] == 1.0
        assert len(d["脉冲"]) == 1
    else:
        # 脉冲先落账：中断随后成功，残额含该脉冲。
        assert results["pulse"] == 200
        assert d["kwh"] == 6.0
        assert len(d["脉冲"]) == 2
    assert d["kwh"] == rec["残额"]["电量"]
    assert rec["对账"]["脉冲电量和相符"] is True
    assert rec["对账"]["残额可复算"] is True
    assert _audit(client)["合计问题"] == 0


# --------------------------------------------------------------------------
# 5. 中断后仍占枪 / 残额静默清零 → 对账必须暴露（模拟脏库）
# --------------------------------------------------------------------------
def test_audit_exposes_stale_occupancy_and_silent_zero(client, open_session):
    sid = open_session(1)["会话编号"]
    gun_id = _get(client, sid)["gun_id"]
    _pulse(client, sid, 2.0)
    _abort(client, sid)

    # 人为制造“中断后仍占枪”
    with db() as conn:
        conn.execute("UPDATE piles SET status='占用' WHERE id=1")
        conn.execute("UPDATE guns SET status='占用' WHERE id=?", (gun_id,))

    a = _audit(client)
    assert a["合计问题"] >= 1
    assert any(x["会话编号"] == sid for x in a["已中断仍占用"])

    # 修复占用后，再制造“残额静默清零”（无事件）
    with db() as conn:
        conn.execute("UPDATE piles SET status='空闲' WHERE id=1")
        conn.execute("UPDATE guns SET status='可用' WHERE id=?", (gun_id,))
        conn.execute("UPDATE sessions SET amount=0 WHERE id=?", (sid,))
    a = _audit(client)
    assert any(x["会话编号"] == sid for x in a["残额静默清零"])


# --------------------------------------------------------------------------
# 6. 无半截双态
# --------------------------------------------------------------------------
def test_no_dual_state_after_abort(client, open_session):
    """中断完成后不允许同时“已中断 + 已关账”或“已中断 + 在充”。"""
    sid = open_session(1)["会话编号"]
    _pulse(client, sid, 1.0)
    _abort(client, sid)

    d = _get(client, sid)
    assert d["status"] == "已中断"
    assert d["settled_at"] is None
    assert d["closed_at"] == d["aborted_at"]

    # 会话不再出现在结算入口的在充桶里
    assert all(x["id"] != sid for x in client.get("/api/会话?状态=充电中").json())
    assert all(x["id"] != sid for x in client.get("/api/会话?状态=结算中").json())
    # 但仍在已中断桶里，可被收口
    assert any(x["id"] == sid for x in client.get("/api/会话?状态=已中断").json())


# --------------------------------------------------------------------------
# 7. 充日“未结”口径与中断待收口联动
# --------------------------------------------------------------------------
def test_dayseal_counts_pending_abort(client, open_session):
    sid = open_session(1)["会话编号"]
    _pulse(client, sid, 2.0)
    _abort(client, sid)

    d = client.get("/api/充日").json()
    assert d["中断待收口数"] == 1
    assert d["中断待收口金额"] == 2.4
    assert d["未结会话数"] == 1  # 已中断未收口仍算未结

    _settle(client, sid)
    d = client.get("/api/充日").json()
    assert d["中断待收口数"] == 0
    assert d["未结会话数"] == 0


# --------------------------------------------------------------------------
# 8. 旧库迁移：缺列时自动补 aborted_at / abort_reason / settled_at
# --------------------------------------------------------------------------
def test_legacy_db_migration_adds_abort_columns(tmp_path):
    import sqlite3

    from app.db import init_db

    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE sessions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slip_no TEXT NOT NULL UNIQUE,
          site_id INTEGER NOT NULL, pile_id INTEGER NOT NULL, gun_id INTEGER NOT NULL,
          rate_plan_id INTEGER NOT NULL, business_day TEXT NOT NULL, status TEXT NOT NULL,
          kwh REAL NOT NULL DEFAULT 0, amount REAL NOT NULL DEFAULT 0, price_locked REAL,
          started_at TEXT NOT NULL, closed_at TEXT, note TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.close()

    import os

    from app import db as dbmod

    old = dbmod.DB_PATH
    os.environ["CHARGEBOUT_DB"] = str(legacy)
    dbmod.DB_PATH = legacy
    try:
        init_db()
    finally:
        dbmod.DB_PATH = old
        os.environ["CHARGEBOUT_DB"] = str(old)

    conn = sqlite3.connect(legacy)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    conn.close()
    assert {"aborted_at", "abort_reason", "settled_at"} <= cols


# --------------------------------------------------------------------------
# 9. 中断后桩位被新会话接占：回执仍按冻结残额重放，且不误报占用
# --------------------------------------------------------------------------
def test_receipt_replay_after_new_occupier(client, open_session):
    sid = open_session(1)["会话编号"]
    _pulse(client, sid, 2.0)
    first = _abort(client, sid).json()
    assert first["占用"]["占用已释放"] is True

    # 新会话接占同一桩位
    nid = open_session(1)["会话编号"]
    assert nid != sid

    again = client.get(f"/api/会话/{sid}/中断回执")
    assert again.status_code == 200
    again = again.json()

    # 中断结局（残额、事件、结算）可重放且完全一致
    assert again["残额"] == first["残额"]
    assert again["脉冲"] == first["脉冲"]
    assert again["结算"] == first["结算"]
    assert again["事件"] == first["事件"]
    assert again["对账"] == first["对账"]

    # 占用如实反映“已被新会话接占”，不是误报残占
    assert again["占用"]["本会话仍占用"] is False
    assert again["占用"]["桩位被新会话接占"] is True
    assert again["占用"]["当前占用会话"] == nid
    assert _audit(client)["已中断仍占用"] == []
