"""进阶体能指标的实测估计（pro_insights 的底层取数）。

只做「从已有活动里挑样本、算指标」，不写判定文案——判定与证据装配在
pro_insights。数据纪律与全引擎一致：样本不足就 available=False 说清原因，
绝不拿均值合成数据编一套看起来正常的结论。
"""
from __future__ import annotations

from datetime import datetime, timedelta

# 爬升 >12m/km 的课受坡度影响大（与 pro_insights.MAX_ELEV_PER_KM 同一口径）
MAX_ELEV_PER_KM = 12.0
# 全程均速快于 40km/h 视为距离字段损坏（同 connections._quarantine_bad_distance 护栏）
MIN_PACE_SEC_PER_KM = 90.0


def _threshold_candidates(runs: list[dict], now: datetime,
                          min_min: float = 30, max_min: float = 70,
                          days: int = 120) -> list[dict]:
    """近 N 个月 30-70min 的连续跑努力，按配速从快到慢。

    含热身的整段均速是乳酸阈配速的保守下限；爬山课剔除，坏距离剔除。
    """
    cands = []
    floor_date = (now - timedelta(days=days)).date().isoformat()
    for a in runs:
        dur, hr, dist = a.get("duration_sec"), a.get("avg_hr"), a.get("distance_m")
        if not (dur and hr and dist):
            continue
        mins = dur / 60
        if not (min_min <= mins <= max_min):
            continue
        d_iso = (a.get("start_time") or now).date().isoformat()
        if d_iso < floor_date:
            continue
        if a.get("elevation_m") and a["elevation_m"] / (dist / 1000) > MAX_ELEV_PER_KM:
            continue
        pace = dur / (dist / 1000)
        if pace < MIN_PACE_SEC_PER_KM:
            continue
        cands.append({
            "pace_sec": round(pace, 1),
            "duration_min": round(mins),
            "date": d_iso,
            "title": a.get("title") or "跑步",
            "avg_hr": hr,
        })
    cands.sort(key=lambda c: c["pace_sec"])
    return cands


def estimate_lthr(runs: list[dict], now: datetime) -> dict:
    """乳酸阈心率下限估计：最快 30-70min 努力的均心率 + 时长修正。

    越短的努力热身稀释越重，均心率离真实阈值越远：30min 约 +3bpm、
    70min 起修正归零，线性过渡。置信度看样本量与前几名心率的一致性。
    """
    cands = _threshold_candidates(runs, now)
    if not cands:
        return {"available": False,
                "unavailable_reason": "近 4 个月没有 30-70min 带心率的连续跑，无法估计乳酸阈心率"}

    best = cands[0]
    correction = max(0.0, (70 - best["duration_min"]) / 40 * 3)
    bpm = int(round(best["avg_hr"] + correction))

    top = cands[:3]
    if len(top) >= 3 and max(t["avg_hr"] for t in top) - min(t["avg_hr"] for t in top) <= 4:
        confidence = "high"
    elif len(top) >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    basis = (f"{best['date']} {best['title']}（{best['duration_min']}min，"
             f"均心率 {best['avg_hr']}，时长修正 +{correction:.0f}bpm）")
    return {
        "available": True,
        "bpm": bpm,
        "display": f"{bpm} bpm",
        "confidence": confidence,
        "basis": f"最快 30-70min 努力：{basis}",
        "samples": cands[:4],
        "evidence": [
            f"{c['date']} {c['title']}：{c['duration_min']}min，"
            f"配速 {c['pace_sec']:.1f}s/km、均心率 {c['avg_hr']}"
            for c in cands[:4]],
    }


def compare_vo2_sources(official: float, local_vdot: float | None) -> dict:
    """官方 VO2max 与本地成绩反推 VDOT 的口径互证。

    |差值| ≤ 1.5 视为一致（工程口径，非生理测量）；official 更高说明
    官方更乐观，反之本地更乐观。本地无基线时 verdict=no_local。
    """
    if local_vdot is None:
        return {"verdict": "no_local", "delta": None}
    delta = round(official - local_vdot, 1)
    if abs(delta) <= 1.5:
        return {"verdict": "aligned", "delta": delta}
    return {"verdict": "official_higher" if delta > 0 else "local_higher", "delta": delta}
