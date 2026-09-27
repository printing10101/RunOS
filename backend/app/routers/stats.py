"""数据报表：月度/年度统计、连续打卡、日历热力图数据、个人纪录（PB）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..data import activities_dicts, get_default_athlete
from ..db import get_db
from ..services.activity_detail import build_activity_detail, rolling_best
from ..services.vdot import time_str

router = APIRouter(prefix="/api/stats", tags=["stats"])

PB_DISTANCES = [("1k", 1000), ("3k", 3000), ("5k", 5000), ("10k", 10000), ("hm", 21097.5)]


@router.get("/report")
def report(db: Session = Depends(get_db)):
    """月度/年度运动报告 + 连续打卡 + 近 12 个月汇总（对标 Keep 运动报告）。"""
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True}
    acts = activities_dicts(db, athlete.id, days=400)

    def summarize(items: list[dict]) -> dict:
        runs = [a for a in items if a["sport"] == "run"]
        km = sum(a["distance_m"] for a in runs) / 1000
        hours = sum(a["duration_sec"] for a in items) / 3600
        return {
            "sessions": len(items),
            "km": round(km, 1),
            "hours": round(hours, 1),
            "elev_m": round(sum(a.get("elevation_m") or 0 for a in items)),
            "calories": sum(a.get("calories") or 0 for a in items),
            "avg_pace_sec": round(sum(a["duration_sec"] for a in runs) / (km or 1)) if km else None,
            "longest_km": round(max((a["distance_m"] / 1000 for a in runs), default=0), 1),
        }

    today = date.today()
    month_acts = [a for a in acts if a["start_time"].date().strftime("%Y-%m") == today.strftime("%Y-%m")]
    year_acts = [a for a in acts if a["start_time"].year == today.year]

    # 近 12 个月汇总
    monthly = []
    for i in range(11, -1, -1):
        m = (today.replace(day=1) - timedelta(days=1))
        for _ in range(i):
            m = (m.replace(day=1) - timedelta(days=1))
        key = m.strftime("%Y-%m")
        items = [a for a in acts if a["start_time"].strftime("%Y-%m") == key]
        s = summarize(items)
        monthly.append({"month": key, **s})

    # 连续打卡（含今天往前，今天未练不打断）
    dates = sorted({a["start_time"].date() for a in acts})
    streak, longest = 0, 0
    if dates:
        d = today if today in dates else today - timedelta(days=1)
        while d in dates:
            streak += 1
            d -= timedelta(days=1)
        run_len, prev = 0, None
        for dt in dates:
            run_len = run_len + 1 if prev and (dt - prev).days == 1 else 1
            longest = max(longest, run_len)
            prev = dt

    return {
        "athlete": {"name": athlete.name},
        "this_month": summarize(month_acts),
        "this_year": summarize(year_acts),
        "monthly": monthly,
        "streak": {"current": streak, "longest": longest},
        "highlights": _highlights(acts, today.year),
    }


def _highlights(acts: list[dict], year: int) -> list[dict]:
    year_acts = [a for a in acts if a["start_time"].year == year and a["sport"] == "run"]
    out = []
    if year_acts:
        longest = max(year_acts, key=lambda a: a["distance_m"])
        out.append({"icon": "🏔", "text": f"最长单次 {longest['distance_m'] / 1000:.1f} km（{longest['title']}）"})
        fastest = min((a for a in year_acts if a["distance_m"] >= 3000),
                      key=lambda a: a["duration_sec"] / (a["distance_m"] or 1), default=None)
        if fastest:
            pace = fastest["duration_sec"] / (fastest["distance_m"] / 1000)
            out.append({"icon": "⚡", "text": f"最快跑步 {fastest['title']}，配速 {time_str(int(pace))}/km"})
        cal = sum(a.get("calories") or 0 for a in year_acts)
        if cal:
            out.append({"icon": "🔥", "text": f"全年消耗约 {cal:,} 千卡"})
    return out


@router.get("/pb")
def personal_records(db: Session = Depends(get_db)):
    """个人纪录：近 120 天跑步分段上的各标准距离最快 contiguous 完成（1k-半马）。"""
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True}
    since = datetime.now() - timedelta(days=120)
    rows = db.scalars(
        select(models.Activity)
        .where(models.Activity.athlete_id == athlete.id,
               models.Activity.sport == "run",
               models.Activity.start_time >= since,
               models.Activity.distance_m >= 1000)
        .order_by(models.Activity.start_time.desc())
    ).all()

    best: dict[str, dict] = {}
    for act in rows:
        # 碎片/异常护栏：配速须在 1:30 ~ 25:00 /km 之间。GPS 漂移碎片
        # （几十米/几十秒）即使因换算错误绕过距离过滤，也不得进 PB。
        pace = act.duration_sec * 1000.0 / act.distance_m if act.duration_sec else 0
        if not (90 <= pace <= 1500):
            continue
        detail = build_activity_detail(act, athlete)
        splits = detail["splits"]["items"]
        if not splits:
            continue
        for key, dist in PB_DISTANCES:
            t = rolling_best(splits, dist)
            if t and (key not in best or t < best[key]["time_sec"]):
                best[key] = {"distance_key": key, "time_sec": t, "time_str": time_str(t),
                             "pace_sec_per_km": round(t / (dist / 1000)),
                             "activity_id": act.id, "activity_title": act.title,
                             "date": act.start_time.date().isoformat()}

    labels = {"1k": "1 公里", "3k": "3 公里", "5k": "5 公里", "10k": "10 公里", "hm": "半程马拉松"}
    items = []
    for key, _ in PB_DISTANCES:
        if key in best:
            items.append({"label": labels[key], **best[key]})
    note = None
    if all(b.get("activity_id") for b in best.values()) and rows:
        note = "PB 取自近 120 天活动内分段的滑窗最快完成时间；同步真实设备分段后精度更高"
    return {"empty": not items, "items": items, "note": note}


@router.get("/calendar")
def calendar_days(days: int = 365, db: Session = Depends(get_db)):
    """日历热力图数据：每日跑量 km + 负荷。"""
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True}
    since = date.today() - timedelta(days=days)
    rows = db.scalars(
        select(models.Activity).where(models.Activity.athlete_id == athlete.id,
                                      models.Activity.start_time >= since)).all()
    by_day: dict[str, dict] = {}
    for r in rows:
        k = r.start_time.date().isoformat()
        d = by_day.setdefault(k, {"date": k, "km": 0, "load": 0, "sessions": 0})
        d["km"] += (r.distance_m or 0) / 1000 if r.sport == "run" else 0
        d["load"] += r.training_load or 0
        d["sessions"] += 1
    return {"items": [{**v, "km": round(v["km"], 1), "load": round(v["load"])} for v in by_day.values()]}
