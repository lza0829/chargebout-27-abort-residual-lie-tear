from __future__ import annotations
from app.services import abort_surface

from fastapi import HTTPException

from app.services.core import current_band_price, get_setting, is_day_sealed, now_str, today

# 急停中断残额口径（全项目唯一口径）：方案 B —— 保留可结算残额。
#
#   * 中断即冻结残额（电量/单价/金额），释放桩位与枪线，并落“异常中断”事件；
#   * 冻结点一经写入不再改写，中断结局可逐项重放（见 abort_receipt）；
#   * 允许对“已中断”会话走一次结算收口（settle），收口后转“已关账”；
#   * 残额不得被静默清零：任何清零都必须有事件与可追的会话留痕。
#
# 本模块之外不得再实现第二套中断语义。

# 可被急停中断的状态
ABORTABLE = ("充电中", "结算中", "已超时")
# 可结算关账的状态（已中断 = 一次收口机会）
SETTLEABLE = ("充电中", "结算中", "已超时", "已中断")
# 仍占着桩位/枪线的会话状态
OPEN_STATUSES = ("充电中", "结算中")

EPS_KWH = 0.0005
EPS_AMOUNT = 0.005


def _next_slip(conn) -> str:
    n = int(get_setting(conn, "会话序号", "1000")) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('会话序号',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(n),),
    )
    return f"充{n}"


def _event(conn, session_id: int, action: str, ts: str, note: str) -> int:
    cur = conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, action, ts, note),
    )
    return int(cur.lastrowid)


def _residual(se) -> dict:
    """按锁定电价复算残额；中断时冻结的金额即由本函数得出。"""
    kwh = round(float(se["kwh"] or 0), 3)
    price = float(se["price_locked"] or 0)
    return {"电量": kwh, "单价": price, "金额": round(kwh * price, 2)}


def _pulse_stats(conn, session_id: int) -> dict:
    r = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(delta_kwh),0) AS k FROM pulses WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return {"条数": int(r["n"]), "电量和": round(float(r["k"]), 3)}


def _holder(conn, pile_id: int) -> int | None:
    r = conn.execute(
        "SELECT id FROM sessions WHERE pile_id=? AND status IN ('充电中','结算中') ORDER BY id LIMIT 1",
        (pile_id,),
    ).fetchone()
    return int(r["id"]) if r else None


def _release_occupancy(conn, se) -> bool:
    """仅当会话仍是桩位/枪线的占用者时才释放，绝不误放后来者的占用。"""
    if _holder(conn, se["pile_id"]) is not None:
        return False
    # 只把“占用”翻回“空闲/可用”；
    # 人工置的故障/维护不会被中断回执或收口改写。
    conn.execute("UPDATE piles SET status='空闲' WHERE id=? AND status='占用'", (se["pile_id"],))
    conn.execute("UPDATE guns SET status='可用' WHERE id=? AND status='占用'", (se["gun_id"],))
    return True


def open_session(conn, pile_id: int, gun_id: int | None = None) -> dict:
    if is_day_sealed(conn):
        raise HTTPException(400, detail="充日已封印，禁止新开充")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    if pile["status"] != "空闲":
        raise HTTPException(400, detail=f"桩位当前为{pile['status']}，不可开充")
    site = conn.execute("SELECT * FROM sites WHERE id=?", (pile["site_id"],)).fetchone()
    if not site or not site["active"]:
        raise HTTPException(400, detail="站点未启用")

    # 桩位空闲但仍有在充会话属于脏数据，宁可拒绝也不叠加第二条。
    if _holder(conn, pile_id) is not None:
        raise HTTPException(400, detail="桩位仍有在充会话，状态不同步")

    if gun_id is None:
        gun = conn.execute(
            "SELECT * FROM guns WHERE pile_id=? AND status='可用' ORDER BY id LIMIT 1",
            (pile_id,),
        ).fetchone()
    else:
        gun = conn.execute(
            "SELECT * FROM guns WHERE id=? AND pile_id=?", (gun_id, pile_id)
        ).fetchone()
    if not gun:
        raise HTTPException(400, detail="无可用枪线")
    if gun["status"] != "可用":
        raise HTTPException(400, detail=f"枪线当前为{gun['status']}")

    price = current_band_price(conn, pile["rate_plan_id"])
    slip = _next_slip(conn)
    ts = now_str()
    cur = conn.execute(
        """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,kwh,amount,price_locked,started_at,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            slip,
            pile["site_id"],
            pile_id,
            gun["id"],
            pile["rate_plan_id"],
            today(),
            "充电中",
            0,
            0,
            price,
            ts,
            "",
        ),
    )
    sid = cur.lastrowid
    conn.execute("UPDATE piles SET status='占用' WHERE id=?", (pile_id,))
    conn.execute("UPDATE guns SET status='占用' WHERE id=?", (gun["id"],))
    _event(conn, sid, "开会话", ts, f"锁定电价{price}")
    return {"会话号": slip, "会话编号": sid, "锁定电价": price, "状态": "充电中"}


def add_pulse(conn, session_id: int, delta_kwh: float, power_kw: float = 0) -> dict:
    if delta_kwh <= 0:
        raise HTTPException(400, detail="脉冲电量须大于零")
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(
            400,
            detail=f"会话状态为{se['status']}，不可记脉冲"
            + ("（已中断会话残额已冻结，请走结算收口）" if se["status"] == "已中断" else ""),
        )
    ts = now_str()
    conn.execute(
        "INSERT INTO pulses(session_id,delta_kwh,power_kw,created_at) VALUES(?,?,?,?)",
        (session_id, delta_kwh, power_kw, ts),
    )
    new_kwh = round(float(se["kwh"]) + float(delta_kwh), 3)
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(new_kwh * price, 2)
    conn.execute(
        "UPDATE sessions SET kwh=?, amount=? WHERE id=?",
        (new_kwh, amount, session_id),
    )
    _event(conn, session_id, "脉冲", ts, f"+{delta_kwh}")
    return {"会话编号": session_id, "累计电量": new_kwh, "预估金额": amount}


def settle(conn, session_id: int) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in SETTLEABLE:
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可关账")
    if se["status"] == "已中断" and se["settled_at"]:
        # 双保险：已中断但已有收口时间，说明状态与留痕不同步。
        raise HTTPException(409, detail="该中断会话已收口，不可重复结算")

    ts = now_str()
    from_abort = se["status"] == "已中断"
    if from_abort:
        price = float(se["price_locked"] or 0)
        if price <= 0:
            raise HTTPException(409, detail="中断会话缺少锁定电价，残额无法复算")
        kwh = round(float(se["kwh"]), 3)
        amount = round(kwh * price, 2)
        # 已中断：只收口残额，停止时间沿用中断时间，收口时间另记。
        conn.execute(
            "UPDATE sessions SET status='已关账', amount=?, price_locked=?, settled_at=? WHERE id=?",
            (amount, price, ts, session_id),
        )
        _event(conn, session_id, "中断结算收口", ts, f"残额{amount}入账（电量{kwh}，单价{price}）")
        receipt_status = "已关账"
    else:
        price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
        kwh = round(float(se["kwh"]), 3)
        amount = round(kwh * price, 2)
        conn.execute(
            "UPDATE sessions SET status='已关账', amount=?, closed_at=?, settled_at=?, price_locked=? WHERE id=?",
            (amount, ts, ts, price, session_id),
        )
        _event(conn, session_id, "结算关账", ts, f"金额{amount}")
        receipt_status = "已关账"

    released = _release_occupancy(conn, se)
    return {
        "会话编号": session_id,
        "电量": kwh,
        "金额": amount,
        "状态": receipt_status,
        "桩位枪线已释放": released,
        "来源": "中断收口" if from_abort else "结算关账",
    }


def mark_timeout(conn, session_id: int) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可标超时")
    minutes = int(get_setting(conn, "超时分钟", "120"))
    ts = now_str()
    conn.execute(
        "UPDATE sessions SET status='已超时', note=? WHERE id=?",
        (f"超过{minutes}分钟未关", session_id),
    )
    _event(conn, session_id, "超时", ts, f"阈值{minutes}分钟")
    return {"会话编号": session_id, "状态": "已超时"}


def abort_session(conn, session_id: int, reason: str = "急停") -> dict:
    """急停中断：冻结残额 + 释放占用 + 落事件，一步到位；可重放、可幂等重入。"""
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")

    if se["status"] == "已关账":
        raise HTTPException(409, detail="会话已关账，不可中断")
    if se["status"] == "已中断":
        # 幂等重入：不重复落事件、不改写冻结点，回执按已冻结数据重放。
        return abort_receipt(conn, session_id)
    if se["status"] not in ABORTABLE:
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可中断")

    ts = now_str()
    price = se["price_locked"]
    if price is None:
        # 冻结点必须自带单价，否则重放不可复算。
        price = current_band_price(conn, se["rate_plan_id"])
    kwh = round(float(se["kwh"] or 0), 3)
    amount = round(kwh * float(price), 2)

    # 单条 UPDATE 完成状态+冻结点，避免“半截中断”的双态。
    conn.execute(
        """UPDATE sessions
           SET status='已中断', kwh=?, amount=?, price_locked=?,
               closed_at=?, aborted_at=?, abort_reason=?, note=?
           WHERE id=?""",
        (kwh, amount, price, ts, ts, reason, reason, session_id),
    )
    released = _release_occupancy(conn, se)
    stats = _pulse_stats(conn, session_id)
    _event(
        conn,
        session_id,
        "异常中断",
        ts,
        f"{reason}；冻结残额 电量{kwh}kWh×单价{price}={amount}"
        f"（{stats['条数']}条脉冲，脉冲电量和{stats['电量和']}）"
        f"{'，桩位枪线已释放' if released else '，桩位枪线由新会话占用未释放'}",
    )
    return abort_receipt(conn, session_id)


def abort_receipt(conn, session_id: int) -> dict:
    """中断回执：全部字段由已落库状态复算，可独立重放核对。"""
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if not se["aborted_at"]:
        raise HTTPException(409, detail="该会话尚无中断记录")

    kwh = round(float(se["kwh"] or 0), 3)
    price = float(se["price_locked"] or 0)
    amount = round(float(se["amount"] or 0), 2)
    recomputed = round(kwh * price, 2)
    stats = _pulse_stats(conn, session_id)
    pile = conn.execute("SELECT status FROM piles WHERE id=?", (se["pile_id"],)).fetchone()
    gun = conn.execute("SELECT status FROM guns WHERE id=?", (se["gun_id"],)).fetchone()
    holder = _holder(conn, se["pile_id"])
    self_holding = holder == session_id
    events = [
        {
            "编号": e["id"],
            "时间": e["created_at"],
            "动作": e["action"],
            "备注": e["note"],
        }
        for e in conn.execute(
            "SELECT * FROM session_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    ]
    return {
        "会话编号": session_id,
        "会话号": se["slip_no"],
        "状态": se["status"],
        "中断": {"时间": se["aborted_at"], "原因": se["abort_reason"]},
        "残额": {"电量": kwh, "单价": price, "金额": amount},
        "脉冲": stats,
        "占用": {
            "桩位编号": se["pile_id"],
            "枪线编号": se["gun_id"],
            "桩位状态": pile["status"] if pile else None,
            "枪线状态": gun["status"] if gun else None,
            "当前占用会话": holder,
            "本会话仍占用": self_holding,
            "占用已释放": holder is None,
            "桩位被新会话接占": holder is not None and holder != session_id,
        },
        "结算": {
            "已收口": bool(se["settled_at"]),
            "收口时间": se["settled_at"],
            "待收口": se["status"] == "已中断",
        },
        "对账": {
            "脉冲电量和相符": abs(stats["电量和"] - kwh) < EPS_KWH,
            "残额可复算": abs(recomputed - amount) < EPS_AMOUNT,
            "冻结金额": amount,
            "复算金额": recomputed,
            "单态": se["status"] in ("已中断", "已关账"),
        },
        "事件": events,
    }


def list_aborted(conn) -> list[dict]:
    """所有留过中断记录的会话（含已收口），供中断页与总览对账。"""
    rs = conn.execute(
        """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
           FROM sessions se
           JOIN piles p ON p.id=se.pile_id
           JOIN guns g ON g.id=se.gun_id
           JOIN sites s ON s.id=se.site_id
           WHERE se.aborted_at IS NOT NULL
           ORDER BY se.id DESC LIMIT 200"""
    ).fetchall()
    out = []
    for se in rs:
        d = {k: se[k] for k in se.keys()}
        d["待收口"] = se["status"] == "已中断" and not se["settled_at"]
        out.append(d)
    return out


def abort_audit(conn) -> dict:
    """全局中断对账：把“已中断仍占枪、残额被清零、半截双态”暴露成清单。"""
    bad_occupancy: list[dict] = []
    zeroed: list[dict] = []
    drifted: list[dict] = []
    aborted = conn.execute(
        "SELECT * FROM sessions WHERE status IN ('已中断','已关账') AND aborted_at IS NOT NULL ORDER BY id"
    ).fetchall()
    # 桩位 → 当前在充会话。有后来者占用属正常，不算“已中断仍占用”。
    open_by_pile = {
        int(r["pile_id"]): int(r["id"])
        for r in conn.execute(
            "SELECT id,pile_id FROM sessions WHERE status IN ('充电中','结算中')"
        ).fetchall()
    }
    for se in aborted:
        pile = conn.execute("SELECT status FROM piles WHERE id=?", (se["pile_id"],)).fetchone()
        gun = conn.execute("SELECT status FROM guns WHERE id=?", (se["gun_id"],)).fetchone()
        holder = open_by_pile.get(int(se["pile_id"]))
        # 只把“仍声称占用”判为问题；人工置的故障/维护属运维态，不算残占。
        still_occupied = (pile and pile["status"] == "占用") or (gun and gun["status"] == "占用")
        if holder is None and still_occupied:
            bad_occupancy.append(
                {
                    "会话编号": se["id"],
                    "会话号": se["slip_no"],
                    "桩位状态": pile["status"] if pile else None,
                    "枪线状态": gun["status"] if gun else None,
                }
            )
        kwh = round(float(se["kwh"] or 0), 3)
        price = float(se["price_locked"] or 0)
        amount = round(float(se["amount"] or 0), 2)
        stats = _pulse_stats(conn, se["id"])
        if abs(stats["电量和"] - kwh) >= EPS_KWH:
            drifted.append(
                {
                    "会话编号": se["id"],
                    "会话号": se["slip_no"],
                    "脉冲电量和": stats["电量和"],
                    "会话电量": kwh,
                }
            )
        # 电量非零而残额归零：即便有收口事件，收口本身也不该把有量残额抹成零。
        if kwh > 0 and amount == 0:
            has_close = conn.execute(
                "SELECT 1 FROM session_events WHERE session_id=? AND action IN ('中断结算收口','结算关账','残额作废')",
                (se["id"],),
            ).fetchone()
            zeroed.append(
                {
                    "会话编号": se["id"],
                    "会话号": se["slip_no"],
                    "电量": kwh,
                    "有收口事件": bool(has_close),
                }
            )
    return {
        "已中断仍占用": bad_occupancy,
        "残额静默清零": zeroed,
        "脉冲与电量漂移": drifted,
        "合计问题": len(bad_occupancy) + len(zeroed) + len(drifted),
    }


def set_pile_status(conn, pile_id: int, status: str) -> dict:
    if status not in ("空闲", "占用", "故障", "维护"):
        raise HTTPException(400, detail="非法桩位状态")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    open_se = conn.execute(
        "SELECT id FROM sessions WHERE pile_id=? AND status IN ('充电中','结算中')",
        (pile_id,),
    ).fetchone()
    if open_se and status in ("故障", "维护"):
        # 底座默认：置故障/维护时若仍有在充，拒绝（后续题可改互斥）
        raise HTTPException(400, detail="仍有在充会话，不可直接改故障或维护")
    if status == "空闲" and open_se:
        raise HTTPException(400, detail="仍有在充会话，不可改空闲")
    conn.execute("UPDATE piles SET status=? WHERE id=?", (status, pile_id))
    return {"桩位编号": pile_id, "状态": status}

_abort_session_impl = abort_session


def abort_session(*args, **kwargs):
    out = _abort_session_impl(*args, **kwargs)
    return abort_surface.after_abort(args[0] if args else None, out, *args[1:], **kwargs)

