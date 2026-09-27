"""前瞻态负荷规划：把「已生成的周期化计划」代入 CTL/ATL/TSB 模型，预测未来体能/疲劳/形态曲线。

思想来自 fitness-scripts 的"规划态 PMC"（前瞻负荷规划）：
训练状态面板回答「过去练得怎样」，本模块回答「照这个计划练下去，比赛日会是什么形态」。

口径约定（与 load_status.build_training_status 保持一致）：
  体能 CTL = 28 天日均负荷的 EWMA（τ=42 天），疲劳 ATL = 7 天日均负荷的 EWMA（τ=7 天），
  形态 TSB = CTL - ATL。种子值直接取当日面板的 fitness/fatigue，保证前端
  「今日形态」与预测曲线起点无缝衔接（种子偏差随 e^(-t/τ) 衰减，28 天后 <50%）。

未来负荷估算复用 load 引擎同一心率口径：按课型假设储备心率比（质量课 0.85、
轻松跑 0.68、长距离 0.70 …），走 Banister TRIMP；力量/核心按 sRPE(RPE=6)。
所有数字均为引擎估算，不使用 LLM 或任意默认配速。
"""
from __future__ import annotations

from datetime import date, timedelta

from .load import banister_trimp, session_rpe

# 课型 -> (储备心率比, RPE)。心率课走 TRIMP，力量/核心走 sRPE。
_TYPE_HRR = {"easy": 0.68, "long": 0.70, "cross": 0.65, "tempo": 0.85,
             "interval": 0.88, "quality": 0.85,
             # 法特莱克/坡度/比赛日等二代课型按质量课/比赛强度取值
             "fartlek": 0.85, "hill": 0.85, "race": 0.90}
_TYPE_RPE = {"strength": 6, "core": 5}


def _ewma(prev: float, load: float, tau: int, dt_days: int = 1) -> float:
    k = 1 - pow(2.718281828459045, -dt_days / tau)
    return prev + (load - prev) * k


def planned_workout_load(session_type: str, duration_min: float, athlete: dict) -> float:
    """按课型与时长估算单节课负荷（与训练负荷引擎同一公式与心率口径）。"""
    minutes = max(0.0, float(duration_min or 0))
    if minutes <= 0 or session_type in ("rest", ""):
        return 0.0
    if session_type in _TYPE_RPE:
        return round(session_rpe(minutes * 60, _TYPE_RPE[session_type]) or 0.0, 1)
    hrr = _TYPE_HRR.get(session_type, 0.70)
    max_hr = athlete.get("max_hr")
    rest_hr = athlete.get("resting_hr")
    if max_hr is None or rest_hr is None or max_hr <= rest_hr:
        # 缺心率基准就不猜：退回 sRPE 口径，并让负荷量级保守（RPE=6 相当于中等偏上）
        return round(session_rpe(minutes * 60, 6) or 0.0, 1)
    est_hr = round(float(rest_hr) + (float(max_hr) - float(rest_hr)) * hrr)
    return round(banister_trimp(minutes * 60, est_hr, athlete) or 0.0, 1)


def project_load(daily_actual: list[dict], planned: list[dict], athlete: dict,
                 fitness: float, fatigue: float, race_date: date | None = None,
                 horizon_days: int = 84) -> dict:
    """生成前瞻负荷序列。

    daily_actual: 训练状态面板的近 84 天每日实际负荷（[{date, load, km}]，升序）；
    planned:      未来计划课（[{date, session_type, duration_min, distance_km, title}]，date >= 今天）；
    fitness/fatigue: 当日面板的 28 天/7 天日均负荷，用作 84 天前 EWMA 种子
    （种子误差经 84 天衰减后 ATL≈0、CTL≤13.5%，且「84 天前状态≈近四周均值」本身是合理假设）。
    """
    today = date.today()

    # 1) 历史段：从 84 天前起前向递归 EWMA（前向递归数值稳定；反推法在 τ=7 天上误差逐日放大 7.5 倍，不可用）
    ctl, atl = float(fitness), float(fatigue)
    by_date = {d["date"]: d for d in daily_actual}
    past = []
    for i in range(len(daily_actual) - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        row = by_date.get(d, {})
        load = row.get("load", 0)
        ctl = _ewma(ctl, load, 42)
        atl = _ewma(atl, load, 7)
        past.append({"date": d, "load": load, "km": row.get("km", 0),
                     "ctl": round(ctl, 1), "atl": round(atl, 1),
                     "tsb": round(ctl - atl, 1), "planned": False})

    plan_by_date: dict[str, dict] = {}
    for w in planned:
        key = w["date"].isoformat() if isinstance(w.get("date"), date) else str(w["date"])
        est = planned_workout_load(w.get("session_type", ""), w.get("duration_min", 0), athlete)
        cur = plan_by_date.setdefault(key, {"load": 0.0, "km": 0.0, "sessions": []})
        cur["load"] += est
        cur["km"] += float(w.get("distance_km") or 0)
        cur["sessions"].append({"session_type": w.get("session_type"), "title": w.get("title", ""),
                                "duration_min": w.get("duration_min"), "load": est})

    series = list(past)
    today_state = {"ctl": round(ctl, 1), "atl": round(atl, 1), "tsb": round(ctl - atl, 1)}
    min_tsb, min_tsb_date = 1e9, ""
    race_tsb = None
    for i in range(horizon_days):
        d = today + timedelta(days=i)
        key = d.isoformat()
        load = round(plan_by_date.get(key, {}).get("load", 0.0), 1)
        km = round(plan_by_date.get(key, {}).get("km", 0.0), 1)
        sessions = plan_by_date.get(key, {}).get("sessions", [])
        ctl = _ewma(ctl, load, 42)
        atl = _ewma(atl, load, 7)
        tsb = ctl - atl
        if d == race_date:
            race_tsb = round(tsb)
        if tsb < min_tsb:
            min_tsb, min_tsb_date = tsb, key
        series.append({"date": key, "load": load, "km": km, "ctl": round(ctl, 1),
                       "atl": round(atl, 1), "tsb": round(tsb, 1), "planned": True,
                       "sessions": sessions if sessions else None})

    # 周汇总（未来）
    weekly = []
    for i in range(0, horizon_days, 7):
        chunk = [s for s in series if s["planned"] and
                 today + timedelta(days=i) <= date.fromisoformat(s["date"]) < today + timedelta(days=i + 7)]
        if not chunk:
            continue
        weekly.append({
            "week_start": chunk[0]["date"],
            "planned_km": round(sum(s["km"] for s in chunk), 1),
            "planned_load": round(sum(s["load"] for s in chunk)),
            "sessions": sum(1 for s in chunk if s["sessions"]),
        })

    warnings: list[str] = []
    if min_tsb < -40:
        warnings.append(f"最深形态 {min_tsb:.0f}（{min_tsb_date}）：疲劳谷过深，超出常规周期化范围（建议谷值 -20 ~ -35），考虑下调峰值周跑量或插入恢复周")
    elif min_tsb < -30:
        warnings.append(f"最深形态 {min_tsb:.0f}（{min_tsb_date}）：接近过劳区间，注意峰值周的睡眠与营养保障")
    streak, best, streak_end = 0, 0, ""
    for s in series:
        if s["planned"] and s["tsb"] < -25:
            streak += 1
            if streak > best:
                best, streak_end = streak, s["date"]
        else:
            streak = 0
    if best >= 14:
        warnings.append(f"连续 {best} 天形态低于 -25：缺少恢复周，建议在 {streak_end} 前后安排减量周")

    verdict = _verdict(warnings, race_tsb, race_date, today)

    return {
        "today": today_state,
        "series": series,
        "weekly": weekly,
        "race": {"date": race_date.isoformat(), "days_to_race": (race_date - today).days,
                 "tsb_on_race": race_tsb} if race_date and today <= race_date <= today + timedelta(days=horizon_days) else None,
        "warnings": warnings,
        "verdict": verdict,
        "note": "未来负荷按课型由负荷引擎估算（TRIMP/sRPE 同一口径）；执行中可随时重新生成对比",
    }


def _verdict(warnings: list[str], race_tsb: float | None,
             race_date: date | None, today: date) -> str:
    if race_tsb is not None:
        if -15 <= race_tsb <= 10:
            base = f"按当前计划执行，比赛日形态 +{race_tsb:.0f}，处于减量后最佳窗口"
        elif race_tsb < -15:
            base = f"比赛日形态 {race_tsb:.0f}：减量不足，赛前疲劳未完全消退，建议赛末周再降负荷"
        else:
            base = f"比赛日形态 +{race_tsb:.0f}：减量幅度偏大，体能有所流失，可适度保留质量课"
    elif race_date and race_date > today:
        base = f"比赛在 {horizon_note(race_date, today)}之后，当前仅投影 12 周内曲线"
    else:
        base = "按当前计划执行，负荷节奏见曲线"
    if warnings:
        base += "；注意：" + warnings[0].split("：", 1)[-1]
    return base


def horizon_note(race_date: date, today: date) -> str:
    return f"{(race_date - today).days} 天"
