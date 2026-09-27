"""训练状态引擎：对标佳明/高驰的训练状态体系（工程近似实现）。

输出：
  - 每日负荷序列（84 天）与 7 天/28 天负荷、ACWR、体能-疲劳-形态
  - 负荷重点（无氧/高强度有氧/低强度有氧 构成，4 周）
  - 训练状态标签（维持/效率良好/效率不佳/巅峰/恢复中/负荷过高/中断）
  - 训练准备度（睡眠/HRV/恢复时间/负荷 合成 0-100，附分量）
  - 恢复时间（小时）
  - 耐力得分 / 爬坡得分（0-100，工程估计）

所有分数均为启发式估计，随数据积累滚动修正；字段缺失时逐项优雅降级。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .load import hr_reserve

# 负荷重点参考区间（占比，工程近似佳明负荷重点思想）
FOCUS_GUIDE = {"low_aerobic": (55, 80), "high_aerobic": (15, 35), "anaerobic": (0, 12)}


def _days_ago(n: int) -> datetime:
    return datetime.now() - timedelta(days=n)


def classify_intensity(avg_hr: int | None, sport: str, athlete: dict) -> str:
    """按储备心率把活动归入三类负荷（Garmin 负荷重点近似）。

    心率口径复用 services.load.hr_reserve，与训练负荷引擎保持同一标准。
    """
    if sport == "strength":
        return "strength"
    pct = hr_reserve(avg_hr, athlete)
    if pct is None:
        return "other"
    if pct >= 0.88:
        return "anaerobic"
    if pct >= 0.75:
        return "high_aerobic"
    return "low_aerobic"


# ---------------------------------------------------------------- 主入口

def build_training_status(acts: list[dict], athlete: dict,
                          body_metrics: list[dict], race_date: date | None = None,
                          vdot_trend: float | None = None) -> dict:
    now = datetime.now()
    runs = [a for a in acts if a.get("sport") == "run"]

    # 1) 每日负荷/跑量序列（近 84 天）
    daily = []
    for i in range(83, -1, -1):
        d = (now - timedelta(days=i)).date()
        day_acts = [a for a in acts if a["start_time"].date() == d]
        daily.append({
            "date": d.isoformat(),
            "load": round(sum(a.get("training_load") or 0 for a in day_acts)),
            "km": round(sum((a.get("distance_m") or 0) / 1000 for a in day_acts if a.get("sport") == "run"), 1),
        })

    def load_since(days: int) -> float:
        cut = now - timedelta(days=days)
        return sum(a.get("training_load") or 0 for a in acts if a["start_time"] >= cut)

    acute = load_since(7)
    chronic_weekly = load_since(28) / 4.0
    acwr = round(acute / chronic_weekly, 2) if chronic_weekly > 0 else None
    fitness = round(load_since(28) / 28)          # 28 天日均负荷 ≈ 体能
    fatigue = round(load_since(7) / 7)            # 7 天日均负荷 ≈ 疲劳
    form = fitness - fatigue                      # 正=状态新鲜，负=疲劳积累

    # Foster 单调性/应变：单调性 = 近 7 天日均负荷 ÷ 日负荷标准差，值越高说明
    # 训练越「千篇一律」（过度训练经典前兆）；应变 = 周总负荷 × 单调性。
    last7 = [d["load"] for d in daily[-7:]]
    mean7 = sum(last7) / 7
    var7 = sum((x - mean7) ** 2 for x in last7) / 7
    if mean7 <= 0:
        monotony = {"monotony": None, "strain": None,
                    "verdict": "近 7 天没有训练负荷，暂无可算的单调性"}
    else:
        m = mean7 / (var7 ** 0.5) if var7 > 1e-9 else 5.0   # 完全均一：单调性顶格
        strain = round(sum(last7) * m)
        m = round(m, 2)
        verdict = ("负荷多样且均衡" if m <= 1.5
                   else "训练安排偏单调，穿插不同强度的课" if m <= 2.0
                   else "高度单调，过度训练风险信号，主动安排强弱交替")
        monotony = {"monotony": m, "strain": strain, "verdict": verdict}

    # 2) 负荷重点（近 28 天时长构成）
    focus = _load_focus(acts, athlete)

    # 3) 恢复时间（小时）
    recovery_h = _recovery_hours(acts, now)

    # 4) 训练状态标签
    status = _training_status(acts, acwr, chronic_weekly, vdot_trend, race_date, now)

    # 5) 训练准备度
    latest_body = max(body_metrics, key=lambda m: m["date"]) if body_metrics else None
    readiness = _readiness(body_metrics, latest_body, athlete, acwr, recovery_h)

    # 6) 耐力得分 / 爬坡得分
    endurance = _endurance_score(runs, now)
    hill = _hill_score(runs, now)

    return {
        "daily": daily,
        "acute_7d": round(acute),
        "chronic_weekly": round(chronic_weekly),
        "acwr": acwr,
        "monotony": monotony,
        "fitness": fitness, "fatigue": fatigue, "form": form,
        "load_focus": focus,
        "recovery_time_h": recovery_h,
        "status": status,
        "readiness": readiness,
        "endurance_score": endurance,
        "hill_score": hill,
        "aerobic_efficiency": aerobic_efficiency(runs, athlete, now),
    }


def aerobic_efficiency(runs: list[dict], athlete: dict,
                       now: datetime | None = None) -> dict:
    """有氧效率 EF 趋势：近 8 周低强度心率跑的 EF（速度 m/min ÷ 均心率）
    对比之前 8 周。同配速下心率越低 EF 越高，逐月上升是有氧底子在变厚。

    只认带均心率的跑步且储备心率 ≤75%（低强度口径，高强度课心率受配速
    结构干扰大）。样本不足时 available=False——8 周窗口是趋势的前提，
    不拿 3 天数据编一个「趋势」。EF 用 m/min 口径（Garmin 同款，值域
    0.5-3 可读）；km/min 口径量级 ~0.001，round(3) 会把差异整个抹掉。
    """
    now = now or datetime.now()
    max_hr, rest_hr = athlete.get("max_hr"), athlete.get("resting_hr")

    def is_easy(a: dict) -> bool:
        if not (a.get("avg_hr") and a.get("distance_m") and a.get("duration_sec")):
            return False
        if a["distance_m"] < 3000 or a["duration_sec"] < 1200:
            return False
        if not (max_hr and rest_hr and max_hr > rest_hr):
            return True   # 无区间参数时不筛强度，只按数据完备性
        pct = (a["avg_hr"] - rest_hr) / (max_hr - rest_hr)
        return pct <= 0.75

    def ef(a: dict) -> float:
        return (a["distance_m"] / (a["duration_sec"] / 60)) / a["avg_hr"]

    recent = [ef(a) for a in runs if (now - a["start_time"]).days < 56 and is_easy(a)]
    prior = [ef(a) for a in runs
             if 56 <= (now - a["start_time"]).days < 112 and is_easy(a)]
    if len(recent) < 5:
        return {"available": False, "ef_recent": None, "delta_pct": None,
                "verdict": f"近 8 周只有 {len(recent)} 次带心率的低强度跑（需 ≥5 次），"
                           "再积累些轻松跑就能看到有氧效率趋势"}
    ef_recent = round(sum(recent) / len(recent), 3)
    if len(prior) < 3:
        return {"available": True, "ef_recent": ef_recent, "delta_pct": None,
                "verdict": f"近 8 周 EF 均值 {ef_recent}，此前样本不足，"
                           "继续积累 8 周后可对照趋势"}
    delta_pct = round((ef_recent - sum(prior) / len(prior)) / (sum(prior) / len(prior)) * 100, 1)
    if delta_pct > 2:
        verdict = f"EF 较前 8 周提升 {delta_pct}%，同配速心率在下降，有氧底子变厚"
    elif delta_pct < -2:
        verdict = f"EF 较前 8 周下降 {abs(delta_pct)}%，可能与疲劳或偏多的强度课有关，" \
                  "检查轻松跑是否跑得够慢"
    else:
        verdict = "EF 与前 8 周基本持平，有氧能力稳定"
    return {"available": True, "ef_recent": ef_recent, "delta_pct": delta_pct,
            "verdict": verdict}


# ---------------------------------------------------------------- 负荷重点

def _load_focus(acts: list[dict], athlete: dict) -> dict:
    """近 28 天负荷重点（跑步/骑行/游泳按时长三分类；力量单独计列，不占分母）。"""
    cut = _days_ago(28)
    buckets = {"low_aerobic": 0.0, "high_aerobic": 0.0, "anaerobic": 0.0}
    strength_h = 0.0
    for a in acts:
        if a["start_time"] < cut:
            continue
        kind = classify_intensity(a.get("avg_hr"), a.get("sport", "run"), athlete)
        if kind == "strength":
            strength_h += a.get("duration_sec", 0) / 3600
        elif kind in buckets:
            buckets[kind] += a.get("duration_sec", 0) / 3600
    total = sum(buckets.values())
    if total <= 0:
        return {"available": False, "verdict": "暂无近 28 天训练数据"}
    pct = {k: round(v / total * 100) for k, v in buckets.items()}
    main = max(("low_aerobic", "high_aerobic", "anaerobic"), key=lambda k: pct[k])
    if pct["anaerobic"] > FOCUS_GUIDE["anaerobic"][1] + 5:
        verdict = "无氧占比偏高：高强度刺激过密，注意安排低强度有氧打底"
    elif pct["low_aerobic"] < FOCUS_GUIDE["low_aerobic"][0]:
        verdict = "低强度有氧不足：轻松跑占比偏低，灰色区间训练偏多"
    elif pct["high_aerobic"] < 10:
        verdict = "强度刺激偏少：可逐步增加节奏跑/阈值课"
    else:
        verdict = "负荷分布均衡，符合极化训练思想"
    return {
        "available": True,
        "hours": {k: round(v, 1) for k, v in buckets.items()},
        "pct": pct,
        "strength_hours": round(strength_h, 1),
        "guide": {k: list(v) for k, v in FOCUS_GUIDE.items()},
        "main": main, "verdict": verdict,
    }


# ---------------------------------------------------------------- 恢复时间

def _recovery_hours(acts: list[dict], now: datetime) -> int:
    """每次训练产生 load/10 小时恢复需求（上限 72h），按每天 24h 线性消耗。"""
    remaining = 0.0
    for a in acts:
        load = a.get("training_load") or 0
        if load <= 0:
            continue
        need = min(72.0, load / 10.0)
        elapsed_h = (now - a["start_time"]).total_seconds() / 3600
        left = need - elapsed_h  # 恢复需求随时间线性消耗（1h/h）
        if left > 0:
            remaining += left
    return int(min(96, round(remaining)))


# ---------------------------------------------------------------- 训练状态

def _training_status(acts: list[dict], acwr: float | None, chronic_weekly: float,
                     vdot_trend: float | None, race_date: date | None, now: datetime) -> dict:
    last_act = max((a["start_time"] for a in acts), default=None)
    if last_act is None or (now - last_act).days >= 10:
        return {"key": "detached", "label": "训练中断", "detail": f"最近一次训练在 {(now - last_act).days if last_act else '—'} 天前，重新开始请从轻松跑起步"}
    if acwr is not None and acwr > 1.5:
        return {"key": "strained", "label": "负荷过高", "detail": f"急慢性负荷比 {acwr}，显著超出安全区间（0.8-1.3），伤病风险上升"}
    if race_date:
        days_to_race = (race_date - now.date()).days
        if 0 <= days_to_race <= 21 and chronic_weekly > 0 and \
                sum(a.get("training_load") or 0 for a in acts if a["start_time"] >= _days_ago(7)) < 0.7 * chronic_weekly:
            return {"key": "peaking", "label": "巅峰期", "detail": f"距比赛 {days_to_race} 天，负荷已下调，体能处于峰值窗口"}
    if acwr is not None and acwr < 0.5:
        return {"key": "recovering", "label": "恢复中", "detail": "近 7 天负荷显著低于常态，减量/休整期"}
    if vdot_trend is not None:
        if vdot_trend > 0.3:
            return {"key": "productive", "label": "效率良好", "detail": f"近 16 周 VDOT 趋势 +{vdot_trend}，训练正在产生回报"}
        if vdot_trend < -0.2 and acwr is not None and acwr > 1.0:
            return {"key": "unproductive", "label": "效率不佳", "detail": f"负荷不低（ACWR {acwr}）但 VDOT 走低，优先检视睡眠与恢复"}
    return {"key": "maintaining", "label": "维持状态", "detail": "负荷与能力大体稳定，按计划继续"}


# ---------------------------------------------------------------- 训练准备度

def _readiness(body_metrics: list[dict], latest: dict | None,
               athlete: dict, acwr: float | None, recovery_h: int) -> dict:
    parts: list[tuple[str, float, float, str]] = []  # (名称, 得分0-1, 权重, 说明)

    if latest:
        sleep = latest.get("sleep_hours")
        if sleep is not None:
            s = 1.0 if 7 <= sleep <= 9 else max(0.2, 1 - abs(sleep - 8) / 3)
            parts.append(("睡眠", s, 0.30, f"昨夜 {sleep:.1f}h"))
        hrv_vals = [m["hrv_rmssd"] for m in body_metrics if m.get("hrv_rmssd") is not None][-7:]
        baseline = athlete.get("hrv_baseline")
        if not baseline:
            # 档案未声明基线时用基线引擎的 28 天中位数（稳健统计，n≥7 才有）；
            # 此前是 hist[-28:-7] 的手写均值兜底，离档天会把基线拖偏
            from .baseline import build_baseline
            baseline = build_baseline(body_metrics)["hrv_rmssd"]["baseline"]
        if hrv_vals and baseline:
            ratio = sum(hrv_vals) / len(hrv_vals) / baseline
            s = 1.0 if 0.95 <= ratio <= 1.10 else max(0.1, 1 - abs(ratio - 1.02) / 0.35)
            tag = "高于基线" if ratio > 1.05 else "低于基线" if ratio < 0.95 else "处于基线区间"
            parts.append(("HRV", s, 0.30, f"近 7 天均值为基线 {ratio:.0%}（{tag}）"))
        stress = latest.get("stress")
        if stress is not None:
            s = 1.0 if stress <= 40 else max(0.1, 1 - (stress - 40) / 60)
            parts.append(("压力", s, 0.10, f"当前压力分数 {stress}"))

    if acwr is not None:
        s = 1.0 if 0.8 <= acwr <= 1.3 else max(0.2, 1 - abs(acwr - 1.05) / 1.2)
        parts.append(("负荷合理性", s, 0.20, f"ACWR {acwr}"))
    if recovery_h is not None:
        s = max(0.0, 1 - recovery_h / 40)
        parts.append(("恢复余量", s, 0.10, f"预计还需恢复 {recovery_h}h"))

    if not parts:
        return {"score": None, "components": [], "verdict": "暂无身体数据，可接入佳明或手动录入后启用"}

    score = round(sum(v * w for _, v, w, _ in parts) / sum(w for _, _, w, _ in parts) * 100)
    verdict = ("状态极佳，可安排高强度课" if score >= 80 else
               "状态良好，正常训练" if score >= 60 else
               "状态一般，建议中等强度" if score >= 40 else
               "疲劳积累，今天以恢复为主")
    return {
        "score": score,
        "components": [{"name": n, "score": round(v * 100), "weight": w, "detail": d} for n, v, w, d in parts],
        "verdict": verdict,
    }


# ---------------------------------------------------------------- 耐力/爬坡得分

def _endurance_score(runs: list[dict], now: datetime) -> dict:
    """耐力得分（0-100）：周跑量 50% + 长距离占比 30% + 规律性 20%（工程估计）。"""
    recent = [a for a in runs if a["start_time"] >= now - timedelta(weeks=12)]
    if not recent:
        return {"score": None, "label": "数据不足"}
    weeks = max(1, len({a["start_time"].date() - timedelta(days=a["start_time"].weekday()) for a in recent}))
    weekly_km = sum((a.get("distance_m") or 0) for a in recent) / 1000 / weeks
    vol_score = min(1.0, weekly_km / 70)                      # 70km/周 = 满分锚点

    by_week: dict[date, float] = {}
    for a in recent:
        wk = a["start_time"].date() - timedelta(days=a["start_time"].weekday())
        by_week[wk] = by_week.get(wk, 0) + (a.get("distance_m") or 0) / 1000
    longs = 0
    for wk, km in by_week.items():
        wk_runs = [a for a in recent
                   if a["start_time"].date() - timedelta(days=a["start_time"].weekday()) == wk]
        longest = max((a.get("distance_m") or 0) / 1000 for a in wk_runs)
        if km > 0 and longest >= max(8.0, km * 0.30):
            longs += 1
    long_score = min(1.0, longs / max(1, len(by_week)) / 0.6)

    regular = sum(1 for wk in by_week
                  if sum(1 for a in recent
                         if a["start_time"].date() - timedelta(days=a["start_time"].weekday()) == wk) >= 3)
    reg_score = min(1.0, regular / max(1, len(by_week)))

    score = round((vol_score * 0.5 + long_score * 0.3 + reg_score * 0.2) * 100)
    label = ("优秀" if score >= 80 else "良好" if score >= 60 else "中等" if score >= 40 else "起步")
    return {"score": score, "label": label,
            "detail": f"周均 {weekly_km:.0f}km · 长距离周占比 {longs}/{len(by_week)} · 规律周 {regular}"}


def _hill_score(runs: list[dict], now: datetime) -> dict:
    """爬坡得分（0-100）：周均爬升 60% + 单位距离爬升 40%（工程估计，锚点 500m/周）。"""
    recent = [a for a in runs if a["start_time"] >= now - timedelta(weeks=8)]
    if not recent:
        return {"score": None, "label": "数据不足"}
    weeks = max(1, len({a["start_time"].date() - timedelta(days=a["start_time"].weekday()) for a in recent}))
    weekly_elev = sum(a.get("elevation_m") or 0 for a in recent) / weeks
    total_km = sum((a.get("distance_m") or 0) for a in recent) / 1000 or 1
    elev_per_km = sum(a.get("elevation_m") or 0 for a in recent) / total_km

    score = round(min(1.0, weekly_elev / 500) * 60 + min(1.0, elev_per_km / 20) * 40)
    label = ("山地型" if score >= 75 else "坡感良好" if score >= 50 else
             "一般" if score >= 25 else "平路型")
    return {"score": score, "label": label,
            "detail": f"周均爬升 {weekly_elev:.0f}m · 每公里爬升 {elev_per_km:.1f}m"}
