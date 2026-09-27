"""饮食分析：按目标与训练负荷给出每日营养目标，评估近期饮食习惯并反馈到综合评估。

训练联动（只出建议不改课表）：
- day_type_for：把计划课型映射为营养日型，让营养目标跟随当天课表动态变化；
- energy_balance：摄入 vs 当日目标/运动消耗的逐日能量平衡；
- fueling_status：能量亏缺的确定性判定，产出可并入每日训练建议的降档与理由。
"""
from __future__ import annotations

from .vocab import is_quality_session


def day_type_for(session_type: str | None) -> str:
    """计划单课 session_type → 营养日型 rest/easy/quality/long，未知课型按常规训练日。

    质量课判定统一走 vocab（含方法库二代词汇 fartlek/hill 与 race），
    不再本地复刻课型映射——vocab 新增质量课词汇时这里自动跟随。
    """
    if is_quality_session(session_type):
        return "quality"
    if session_type == "long":
        return "long"
    if session_type == "rest":
        return "rest"
    return "easy"


def daily_targets(weight_kg: float, height_cm: float, age: float, sex: str,
                  weekly_km: float, day_type: str = "easy") -> dict:
    """day_type: rest / easy / quality / long"""
    if sex == "male":
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    else:
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161
    activity_factor = 1.4 + min(0.45, weekly_km / 400)     # 跑量折算活动系数
    tdee = bmr * activity_factor

    kcal_mult = {"rest": 0.9, "easy": 1.0, "quality": 1.05, "long": 1.1}.get(day_type, 1.0)
    kcal = tdee * kcal_mult
    protein = weight_kg * (2.0 if day_type in ("quality", "long") else 1.7)
    fat = weight_kg * 1.0
    carb_mult = {"rest": 3.5, "easy": 4.5, "quality": 6.5, "long": 7.5}.get(day_type, 4.5)
    carb = weight_kg * carb_mult
    # 未识别的 day_type 落到默认提示，避免 KeyError
    note = {
        "rest": "休息日适度控制碳水，保证蛋白质",
        "easy": "常规训练日：碳水为主能量，蛋白 1.7g/kg",
        "quality": "质量课前 2-3h 补碳水，课后 30min 内蛋白+碳水窗口",
        "long": "长距离日：前一日碳水加载，途中每 45min 补 30-60g 碳水",
    }.get(day_type, "按当日训练类型调整碳水与蛋白质，保证每餐有优质蛋白与足量蔬果")
    return {
        "kcal": round(kcal), "protein_g": round(protein), "carb_g": round(carb), "fat_g": round(fat),
        "bmr": round(bmr), "tdee": round(tdee), "note": note,
    }


def analyze_diet(logs: list[dict], targets: dict, day_targets: dict[str, dict] | None = None) -> dict:
    """近 N 天日志 vs 目标 → 饮食习惯分（0-100）+ 具体问题。

    day_targets: 可选 {date_iso: 目标dict}，按当天课型给出逐日目标；缺的日期回退全局 targets。
    """
    if not logs:
        return {"score": None, "days": 0, "issues": ["暂无饮食记录，无法评估"], "highlights": []}
    by_date: dict[str, dict] = {}
    for entry in logs:
        d = by_date.setdefault(entry["date"], {"kcal": 0, "protein": 0, "carb": 0, "fat": 0, "meals": 0})
        d["kcal"] += entry.get("kcal") or 0
        d["protein"] += entry.get("protein_g") or 0
        d["carb"] += entry.get("carb_g") or 0
        d["meals"] += 1

    days = len(by_date)
    kcal_hits, protein_hits = 0, 0
    for iso, d in by_date.items():
        t = (day_targets or {}).get(iso) or targets
        if t["kcal"] * 0.8 <= d["kcal"] <= t["kcal"] * 1.2:
            kcal_hits += 1
        if d["protein"] >= t["protein_g"] * 0.9:
            protein_hits += 1

    kcal_rate, protein_rate = kcal_hits / days, protein_hits / days
    log_rate = min(1.0, len(logs) / (days * 2.5))   # 记录完整度粗估

    score = round(kcal_rate * 40 + protein_rate * 40 + log_rate * 20)
    issues, highlights = [], []
    if kcal_rate < 0.6:
        issues.append(f"仅 {int(kcal_rate*100)}% 的天数热量达标（目标 ±20%），训练量大时易造成恢复不足")
    if protein_rate < 0.6:
        issues.append(f"仅 {int(protein_rate*100)}% 的天数蛋白质达 {targets['protein_g']}g，影响肌肉修复与力量增长")
    if log_rate < 0.6:
        issues.append("饮食记录不完整，建议至少记录主餐")
    if kcal_rate >= 0.8:
        highlights.append("热量摄入稳定，支撑当前训练量")
    if protein_rate >= 0.8:
        highlights.append("蛋白质摄入充足，利于力量与恢复")

    return {
        "score": score, "days": days,
        "kcal_rate": round(kcal_rate * 100), "protein_rate": round(protein_rate * 100),
        "issues": issues, "highlights": highlights,
        "targets": targets,
    }


def energy_balance(daily: list[dict]) -> dict:
    """逐日能量平衡。daily 按 date 升序：[{date, day_type, intake_kcal, target_kcal, burned_kcal}]。

    balance = 摄入 − 当日目标（>0 盈余 / <0 缺口）；burned 为运动消耗，供图表分层展示。
    """
    series = []
    for d in daily:
        intake, target, burned = (d.get("intake_kcal") or 0), (d.get("target_kcal") or 0), (d.get("burned_kcal") or 0)
        series.append({
            "date": d["date"], "day_type": d.get("day_type", "easy"),
            "intake_kcal": round(intake), "target_kcal": round(target), "burned_kcal": round(burned),
            "balance": round(intake - target),
        })
    last7 = [s for s in series if s["intake_kcal"] > 0][-7:]
    n = max(1, len(last7))
    def avg(k):
        return round(sum(s[k] for s in last7) / n)
    return {"series": series, "recorded_days": len(last7),
            "avg7": {"intake_kcal": avg("intake_kcal"), "target_kcal": avg("target_kcal"),
                     "burned_kcal": avg("burned_kcal"),
                     "balance": round(sum(s["balance"] for s in last7) / n)} if last7 else None}


def fueling_status(history: list[dict], today_day_type: str = "easy",
                   weight_kg: float | None = None) -> dict:
    """补给状态判定（确定性规则）：能量亏缺时产出训练建议的降档与理由。

    history: 截至昨天、按 date 升序的 [{date, intake_kcal, target_kcal, carb_g}]（当天未结束不参与）。
    规则从严到宽：
    - 近 3 个有记录日摄入/目标 < 75% → deficit，建议档位封顶 easy；
    - 昨日摄入 < 80% 目标且今天是质量课/长距离 → low，封顶 reduce；
    - 今天长距离且昨日碳水 < 5g/kg → 仅给补给提示，不降档。
    """
    recorded = [d for d in history if (d.get("intake_kcal") or 0) > 0 and (d.get("target_kcal") or 0) > 0]
    if not recorded:
        return {"level": "unknown", "cap_level": None, "reasons": [], "notes": []}

    reasons: list[str] = []
    notes: list[str] = []
    cap = None
    yesterday = recorded[-1]

    recent3 = recorded[-3:]
    if len(recent3) >= 2:
        ratio = sum(d["intake_kcal"] for d in recent3) / max(1e-9, sum(d["target_kcal"] for d in recent3))
        if ratio < 0.75:
            cap = "easy"
            reasons.append(f"近 {len(recent3)} 个记录日摄入仅为目标 {round(ratio*100)}%，存在持续能量缺口，"
                           "恢复与糖原储备不足，不宜上强度")
    if yesterday["intake_kcal"] < yesterday["target_kcal"] * 0.8 and today_day_type in ("quality", "long"):
        cap = cap or "reduce"
        reasons.append(f"昨日摄入 {round(yesterday['intake_kcal'])} kcal，低于当日目标（{round(yesterday['target_kcal'])}），"
                       "糖原储备可能偏低")
    if today_day_type == "long" and weight_kg and yesterday.get("carb_g") is not None \
            and yesterday["carb_g"] < 5 * weight_kg:
        notes.append(f"昨日碳水 {round(yesterday['carb_g'])}g 低于长距离日建议的加载量（约 5-8g/kg），"
                     "途中注意补给")

    level = "deficit" if cap == "easy" else ("low" if cap or reasons else "ok")
    if not reasons and not notes:
        notes.append("近期摄入达标，补给状态良好")
    return {"level": level, "cap_level": cap, "reasons": reasons, "notes": notes}
