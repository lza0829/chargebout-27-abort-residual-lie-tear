"""方案 B（残额钉枪缺口口径）的可核缺口与假急停收口回归。

覆盖题面五夹具与全局约束：
1. 急停绿枪线仍钉 → 登记“枪线未释放”缺口，急停宣称冻结，三处不许亮齐；
2. 残额被抹零且无事件 → 登记“残额静默清零”缺口，关账入口拒绝吃进；
3. 详情 / 中断页 / 总览 / 回执 四处分叉必须同源同值；
4. 急停与脉冲交错的半截：事务原子回滚，态字改了枪仍钉不许发生；
5. 急停关账想吞抹零残额 → 409，总览金额桶不假绿；
另含：超时入口抢先收口的收敛、急停与关账并发不双挂、缺口对账核销、旁路毒快照清理。
"""
from __future__ import annotations

import threading

from app.db import db
from app.services import sessions as svc


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
    return client.get("/api/总览").json()


def _audit(client) -> dict:
    return client.get("/api/中断对账").json()


def _receipt(client, sid: int) -> dict:
    return client.get(f"/api/会话/{sid}/中断回执").json()


def _aborted_rows(client) -> list[dict]:
    return client.get("/api/中断会话").json()


def _pin_guns(client, sid: int):
    """模拟“急停成功灯亮、枪线却仍被钉住”的脏证据。"""
    d = _get(client, sid)
    with db() as conn:
        conn.execute("UPDATE piles SET status='占用' WHERE id=?", (d["pile_id"],))
        conn.execute("UPDATE guns SET status='占用' WHERE id=?", (d["gun_id"],))


def _zero_residual(client, sid: int):
    """模拟已记电量金额被抹成零、且没有任何负向事件。"""
    with db() as conn:
        conn.execute("UPDATE sessions SET amount=0 WHERE id=?", (sid,))


# --------------------------------------------------------------------------
# 夹具 1：急停绿枪线仍钉 → 可核缺口冻结急停宣称，三处不亮齐
# --------------------------------------------------------------------------
def test_pinned_gun_after_abort_opens_gap_and_freezes_claim(client, open_session):
    sid = open_session(1)["会话编号"]
    _pulse(client, sid, 2.0)
    r = _abort(client, sid)
    assert r.status_code == 200
    assert r.json()["急停宣称"] is True  # 正常中断：证据齐全，绿灯成立

    _pin_guns(client, sid)

    # 对账把钉枪核成持久化缺口
    a = _audit(client)
    assert any(x["会话编号"] == sid for x in a["已中断仍占用"])
    assert any(g["会话编号"] == sid and g["类型"] == "枪线未释放" for g in a["未清缺口"])
    assert a["未清缺口数"] >= 1

    # 回执：急停宣称冻结，收口被冻结
    rec = _receipt(client, sid)
    assert rec["急停宣称"] is False
    assert rec["对账"]["无未清缺口"] is False
    assert any(g["类型"] == "枪线未释放" for g in rec["缺口"])
    assert rec["结算"]["收口冻结"] is True

    # 详情 / 中断页 同源看到缺口
    d = _get(client, sid)
    assert d["急停宣称"] is False
    assert any(g["类型"] == "枪线未释放" for g in d["未清缺口"])
    row = next(x for x in _aborted_rows(client) if x["id"] == sid)
    assert row["急停宣称"] is False
    assert row["缺口数"] == 1

    # 总览已中断桶不许亮齐
    o = _overview(client)
    assert o["已中断齐"] is False
    assert o["未清缺口数"] >= 1
    assert o["缺口会话数"] >= 1

    # 缺口未清，关账入口拒绝
    blocked = _settle(client, sid)
    assert blocked.status_code == 409
    assert "缺口" in blocked.json()["detail"]

    # 补齐证据（释放枪线）后对账核销，宣称恢复、可收口
    with db() as conn:
        conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (d["pile_id"],))
        conn.execute("UPDATE guns SET status='可用' WHERE id=?", (d["gun_id"],))
    rec_after = client.post("/api/缺口对账", json={"会话编号": sid})
    assert rec_after.status_code == 200
    assert any(x["会话编号"] == sid for x in rec_after.json()["已核销"])
    assert _receipt(client, sid)["急停宣称"] is True
    assert _overview(client)["已中断齐"] is True
    assert _settle(client, sid).status_code == 200


# --------------------------------------------------------------------------
# 夹具 2 + 5：残额抹零无事件 → 缺口登记，关账拒绝，总览不假绿
# --------------------------------------------------------------------------
def test_zeroed_residual_blocks_settle_and_overview_not_green(client, open_session):
    sid = open_session(1)["会话编号"]
    _pulse(client, sid, 2.0)
    frozen = _abort(client, sid).json()["残额"]["金额"]
    assert frozen > 0

    _zero_residual(client, sid)
    a = _audit(client)
    assert any(x["会话编号"] == sid for x in a["残额静默清零"])
    assert any(g["类型"] == "残额静默清零" for g in a["未清缺口"])

    rec = _receipt(client, sid)
    assert rec["急停宣称"] is False
    assert any(g["类型"] == "残额静默清零" for g in rec["缺口"])

    # 关账入口想吃进抹零残额：拒绝
    blocked = _settle(client, sid)
    assert blocked.status_code == 409

    # 总览金额桶展示的就是被抹后的事实（0），且已中断齐为假——不许假绿
    o = _overview(client)
    assert o["已中断齐"] is False
    assert o["未清缺口数"] >= 1
    assert o["已中断残额金额"] == 0

    # 合法“残额作废”事件留痕后缺口可核销；但残额事实未被改写，
    # 收口仍按冻结电量×锁定单价复算，金额不会凭空消失或双挂。
    with db() as conn:
        conn.execute(
            "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
            (sid, "残额作废", svc.now_str(), "人工核废留痕（残额事实仍在，待业务另案处理）"),
        )
    client.post("/api/缺口对账", json={"会话编号": sid})
    assert _receipt(client, sid)["急停宣称"] is True
    settled = _settle(client, sid)
    assert settled.status_code == 200
    assert settled.json()["金额"] == frozen
    # 关账金额桶只入这一笔
    assert _overview(client)["本日关账金额"] == frozen


# --------------------------------------------------------------------------
# 夹具 3：详情 / 中断页 / 总览 / 回执 四处同源，不分叉
# --------------------------------------------------------------------------
def test_four_surfaces_share_one_source_of_truth(client, open_session):
    sid = open_session(1)["会话编号"]
    _pulse(client, sid, 3.0)
    frozen = _abort(client, sid).json()

    d = _get(client, sid)
    row = next(x for x in _aborted_rows(client) if x["id"] == sid)
    o = _overview(client)
    rec = _receipt(client, sid)

    # 残额金额四处一致
    assert d["中断"]["残额"]["金额"] == frozen["残额"]["金额"]
    assert row["amount"] == frozen["残额"]["金额"]
    assert o["已中断残额金额"] == frozen["残额"]["金额"]
    assert rec["残额"]["金额"] == frozen["残额"]["金额"]
    # 电量四处一致
    assert d["kwh"] == row["kwh"] == o["已中断残额电量"] == rec["残额"]["电量"]
    # 状态字四处一致
    assert d["status"] == row["status"] == rec["状态"] == "已中断"
    # 无缺口时四处宣称一致为真
    assert d["急停宣称"] is row["急停宣称"] is rec["急停宣称"] is True
    assert o["已中断齐"] is True
    # 总览桶计数与中断页名单条数一致
    assert o["已中断"] == len(_aborted_rows(client))


# --------------------------------------------------------------------------
# 夹具 4：半截回滚——中断事务中途失败必须整笔回滚，不许态字已改枪仍钉
# --------------------------------------------------------------------------
def test_abort_failure_rolls_back_everything(client, open_session, monkeypatch):
    sid = open_session(1)["会话编号"]
    gun_id = _get(client, sid)["gun_id"]
    _pulse(client, sid, 1.0)

    # 在“态字 UPDATE 之后、释放枪线之前”炸异常：write_db 必须整笔回滚。
    orig = svc._release_occupancy

    def boom(conn, se):
        raise RuntimeError("模拟枪线接口中断")

    monkeypatch.setattr(svc, "_release_occupancy", boom)
    # TestClient 默认把服务端异常抛到调用方；这里要验证 HTTP 500 与事务回滚。
    from fastapi.testclient import TestClient

    quiet = TestClient(client.app, raise_server_exceptions=False)
    r = quiet.post(f"/api/会话/{sid}/中断", json={"原因": "急停"})
    assert r.status_code == 500
    monkeypatch.setattr(svc, "_release_occupancy", orig)

    d = _get(client, sid)
    assert d["status"] == "充电中"          # 态字回滚
    assert d["aborted_at"] is None           # 中断留痕回滚
    assert d["amount"] > 0                   # 残额未被改写
    for p in client.get("/api/桩位").json():
        if p["id"] == d["pile_id"]:
            assert p["status"] == "占用"     # 枪线仍是原会话合法占用，非“半截钉枪”
    gun = next(g for g in client.get("/api/枪线").json() if g["id"] == gun_id)
    assert gun["status"] == "占用"
    # 没有异常中断事件、没有缺口
    assert all(e["action"] != "异常中断" for e in d["事件"])
    assert _audit(client)["合计问题"] == 0

    # 同一笔会话随后仍可正常急停并收敛
    ok = _abort(client, sid)
    assert ok.status_code == 200
    assert ok.json()["急停宣称"] is True


# --------------------------------------------------------------------------
# 并发：急停与关账对撞，只有一个结局，残额不双挂
# --------------------------------------------------------------------------
def test_concurrent_abort_and_settle_no_double_booking(client, open_session):
    sid = open_session(1)["会话编号"]
    _pulse(client, sid, 2.0)
    results: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def do_abort():
        barrier.wait()
        results["abort"] = _abort(client, sid).status_code

    def do_settle():
        barrier.wait()
        results["settle"] = _settle(client, sid).status_code

    t1 = threading.Thread(target=do_abort)
    t2 = threading.Thread(target=do_settle)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    d = _get(client, sid)
    # BEGIN IMMEDIATE 串行化两个写事务，只可能收敛到两种合法结局：
    #  A. 关账先成 → 已关账，急停被拒（409）；
    #  B. 急停先成 → 已中断，随后那笔关账按中断口径收口（200）或撞上状态守卫被拒。
    if results["settle"] == 200 and results["abort"] != 200:
        assert d["status"] == "已关账"
        assert _abort(client, sid).status_code in (400, 409)
    else:
        assert results["abort"] == 200
        assert d["status"] in ("已中断", "已关账")
        if d["status"] == "已中断":
            assert results["settle"] in (400, 409)
    # 残额不双挂：收口类事件至多一条，金额只入一次账，审计干净
    terminal = [e["action"] for e in d["事件"] if e["action"] in ("结算关账", "中断结算收口", "异常中断")]
    assert terminal.count("异常中断") <= 1
    assert terminal.count("结算关账") + terminal.count("中断结算收口") <= 1
    if d["status"] == "已关账":
        assert _overview(client)["本日关账金额"] == d["amount"]
    assert _audit(client)["合计问题"] == 0


# --------------------------------------------------------------------------
# 超时入口抢先收口：超时后急停仍须收敛到同一套缺口口径
# --------------------------------------------------------------------------
def test_timeout_then_abort_converges(client, open_session):
    sid = open_session(1)["会话编号"]
    _pulse(client, sid, 1.0)
    assert client.post(f"/api/会话/{sid}/超时", json={}).status_code == 200

    r = _abort(client, sid)
    assert r.status_code == 200
    rec = r.json()
    assert rec["状态"] == "已中断"
    assert rec["急停宣称"] is True
    d = _get(client, sid)
    assert d["status"] == "已中断"
    assert d["aborted_at"] is not None
    # 超时事件与异常中断事件都在，且审计无缺口
    actions = [e["action"] for e in d["事件"]]
    assert "超时" in actions and "异常中断" in actions
    assert _audit(client)["合计问题"] == 0


# --------------------------------------------------------------------------
# 旁路毒快照：旧 surface_* 设置在初始化时必须被清掉，总览不再认假快照
# --------------------------------------------------------------------------
def test_legacy_surface_poison_purged(client, open_session):
    sid = open_session(1)["会话编号"]
    _pulse(client, sid, 1.0)
    _abort(client, sid)

    # 人工塞进旧旁路的毒数据
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('surface_last',?)",
            ('{"ok": true, "已中断": 99, "桩位占用": 7}',),
        )
        conn.execute("INSERT INTO settings(key,value) VALUES('surface_ok','1')")
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('abort_surface:1',?)",
            ('{"ok": true}',),
        )

    from app.db import init_db

    init_db()  # 重新初始化应清毒
    o = _overview(client)
    assert "surface_ok" not in o
    assert "surface_已中断" not in o
    assert o["已中断"] == 1          # 事实表口径，不认 99
    assert o["桩位占用"] == 0
    with db() as conn:
        keys = {
            r["key"]
            for r in conn.execute(
                "SELECT key FROM settings WHERE key LIKE 'surface%' OR key LIKE 'abort_surface:%'"
            ).fetchall()
        }
    assert keys == set()
    # 回执里也不允许带任何 surface 杂质
    assert "surface_ok" not in _receipt(client, sid)
