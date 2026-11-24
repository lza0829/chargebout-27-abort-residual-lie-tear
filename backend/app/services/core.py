from __future__ import annotations
from datetime import datetime, timedelta

from app.db import db, row_dict, rows


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_setting(conn, key: str, default: str = "") -> str:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def current_band_price(conn, rate_plan_id: int) -> float:
    plan = conn.execute("SELECT * FROM rate_plans WHERE id=?", (rate_plan_id,)).fetchone()
    if not plan:
        return 0.0
    hour = datetime.now().hour
    if 8 <= hour < 11 or 18 <= hour < 21:
        return float(plan["peak_price"])
    if 23 <= hour or hour < 7:
        return float(plan["valley_price"])
    return float(plan["flat_price"])


def is_day_sealed(conn, day: str | None = None) -> bool:
    d = day or today()
    r = conn.execute("SELECT sealed FROM day_seals WHERE business_day=?", (d,)).fetchone()
    return bool(r and r["sealed"])


def overview(conn) -> dict:
    piles = rows(conn.execute("SELECT status, COUNT(*) AS n FROM piles GROUP BY status").fetchall())
    sess = rows(conn.execute("SELECT status, COUNT(*) AS n FROM sessions GROUP BY status").fetchall())
    pile_map = {p["status"]: p["n"] for p in piles}
    sess_map = {s["status"]: s["n"] for s in sess}
    open_kwh = conn.execute(
        "SELECT COALESCE(SUM(kwh),0) AS k FROM sessions WHERE status IN ('充电中','结算中')"
    ).fetchone()["k"]
    today_amount = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS a FROM sessions WHERE business_day=? AND status='已关账'",
        (today(),),
    ).fetchone()["a"]
    # 已中断桶口径与中断页/详情同源：残额取冻结后的 sessions.amount，不在这里另行清零。
    aborted = conn.execute(
        """SELECT COUNT(*) AS n, COALESCE(SUM(kwh),0) AS k, COALESCE(SUM(amount),0) AS a,
                  COALESCE(SUM(CASE WHEN settled_at IS NULL THEN 1 ELSE 0 END),0) AS pending
           FROM sessions WHERE status='已中断'"""
    ).fetchone()
    # 方案 B：未清可核缺口计数。缺口未清，已中断桶不许亮“齐”。
    open_gaps = conn.execute(
        "SELECT COUNT(*) AS n FROM abort_gaps WHERE cleared_at IS NULL"
    ).fetchone()["n"]
    gap_sessions = conn.execute(
        """SELECT COUNT(DISTINCT session_id) AS n FROM abort_gaps WHERE cleared_at IS NULL"""
    ).fetchone()["n"]
    return {
        "桩位空闲": pile_map.get("空闲", 0),
        "桩位占用": pile_map.get("占用", 0),
        "桩位故障": pile_map.get("故障", 0),
        "桩位维护": pile_map.get("维护", 0),
        "充电中": sess_map.get("充电中", 0),
        "结算中": sess_map.get("结算中", 0),
        "已关账": sess_map.get("已关账", 0),
        "已超时": sess_map.get("已超时", 0),
        "已中断": int(aborted["n"]),
        "已中断待收口": int(aborted["pending"]),
        "已中断残额电量": round(float(aborted["k"]), 3),
        "已中断残额金额": round(float(aborted["a"]), 2),
        "未清缺口数": int(open_gaps),
        "缺口会话数": int(gap_sessions),
        "已中断齐": int(open_gaps) == 0,
        "在充电量": round(float(open_kwh), 3),
        "本日关账金额": round(float(today_amount), 2),
        "营业日": today(),
        "充日已封印": is_day_sealed(conn),
    }


def list_sites(conn):
    return rows(conn.execute("SELECT * FROM sites ORDER BY id").fetchall())


def list_piles(conn, site_id: int | None = None):
    if site_id:
        rs = conn.execute(
            """SELECT p.*, s.name AS site_name, r.name AS rate_name
               FROM piles p JOIN sites s ON s.id=p.site_id
               JOIN rate_plans r ON r.id=p.rate_plan_id
               WHERE p.site_id=? ORDER BY p.id""",
            (site_id,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT p.*, s.name AS site_name, r.name AS rate_name
               FROM piles p JOIN sites s ON s.id=p.site_id
               JOIN rate_plans r ON r.id=p.rate_plan_id
               ORDER BY p.id"""
        ).fetchall()
    return rows(rs)


def list_guns(conn, pile_id: int | None = None):
    if pile_id:
        return rows(
            conn.execute(
                "SELECT g.*, p.name AS pile_name FROM guns g JOIN piles p ON p.id=g.pile_id WHERE g.pile_id=? ORDER BY g.id",
                (pile_id,),
            ).fetchall()
        )
    return rows(
        conn.execute(
            "SELECT g.*, p.name AS pile_name FROM guns g JOIN piles p ON p.id=g.pile_id ORDER BY g.id"
        ).fetchall()
    )


def list_rates(conn):
    return rows(conn.execute("SELECT * FROM rate_plans ORDER BY id").fetchall())


def list_sessions(conn, status: str | None = None):
    if status:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               WHERE se.status=? ORDER BY se.id DESC""",
            (status,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               ORDER BY se.id DESC LIMIT 200"""
        ).fetchall()
    out = rows(rs)
    # 列表与中断页/总览同源带上缺口计数，结算入口据此冻结收口按钮。
    gap_counts = {
        int(r["session_id"]): int(r["n"])
        for r in conn.execute(
            "SELECT session_id, COUNT(*) AS n FROM abort_gaps WHERE cleared_at IS NULL GROUP BY session_id"
        ).fetchall()
    }
    for d in out:
        n = gap_counts.get(int(d["id"]), 0)
        d["缺口数"] = n
        d["急停宣称"] = (n == 0) if d.get("aborted_at") else False
    return out


def session_detail(conn, session_id: int):
    se = conn.execute(
        """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name, r.name AS rate_name
           FROM sessions se
           JOIN piles p ON p.id=se.pile_id
           JOIN guns g ON g.id=se.gun_id
           JOIN sites s ON s.id=se.site_id
           JOIN rate_plans r ON r.id=se.rate_plan_id
           WHERE se.id=?""",
        (session_id,),
    ).fetchone()
    if not se:
        return None
    pulses = rows(
        conn.execute(
            "SELECT * FROM pulses WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    events = rows(
        conn.execute(
            "SELECT * FROM session_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    d = row_dict(se)
    d["脉冲"] = pulses
    d["事件"] = events
    d["脉冲电量和"] = round(sum(float(p["delta_kwh"]) for p in pulses), 3)
    d["可结算"] = se["status"] in ("充电中", "结算中", "已超时", "已中断")
    d["可中断"] = se["status"] in ("充电中", "结算中", "已超时")
    if se["aborted_at"]:
        # 详情页与中断页/回执同源，不另算一套残额；缺口也同源读取，禁止展示层另判。
        gaps = [
            {"类型": r["gap_type"], "明细": r["detail"], "登记时间": r["created_at"]}
            for r in conn.execute(
                "SELECT * FROM abort_gaps WHERE session_id=? AND cleared_at IS NULL ORDER BY id",
                (session_id,),
            ).fetchall()
        ]
        d["未清缺口"] = gaps
        d["急停宣称"] = len(gaps) == 0
        # 详情页与中断页/回执同源，不另算一套残额。
        d["中断"] = {
            "时间": se["aborted_at"],
            "原因": se["abort_reason"],
            "残额": {
                "电量": round(float(se["kwh"] or 0), 3),
                "单价": float(se["price_locked"] or 0),
                "金额": round(float(se["amount"] or 0), 2),
            },
            "待收口": se["status"] == "已中断",
            "收口时间": se["settled_at"],
            "未清缺口": gaps,
            "急停宣称": len(gaps) == 0,
        }
    else:
        d["中断"] = None
        d["未清缺口"] = []
        d["急停宣称"] = False
    return d

