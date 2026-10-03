"""减量（taper）对照：赛末 3 周的计划负荷路径 vs 推荐衰减带。

比赛周照常重负荷是赛前最大的可避免错误。本引擎把当前计划的赛末几周
负荷换算成「峰值周的百分比」，逐周对照推荐衰减带（比赛周 30-60%、
赛前 1 周 45-75%、赛前 2 周 60-85%），只出「达标/偏高/偏低」的档位与
理由，不改课表。单周负荷按课型用 load_project.planned_workout_load 估算
（与前瞻负荷规划同一口径）；峰值周取全计划各周计划负荷的最大值。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .load_project import planned_workout_load
from .planner import week_start  # 全项目统一的「这一周的周一」单源（重导出供测试引用）

# 赛末周 → 推荐衰减带（占峰值周计划负荷的比例）
TAPER_BANDS = {
    0: (0.30, 0.60),   # 比赛周
    1: (0.45, 0.75),   # 赛前 1 周
    2: (0.60, 0.85),   # 赛前 2 周
}
_TAPER_WINDOW_WEEKS = 3
_MIN_PEAK_LOAD = 100.0   # 峰值周负荷低于此视为计划太薄，比衰减率没有意义

_WEEK_LABELS = {0: "比赛周", 1: "赛前 1 周", 2: "赛前 2 周"}


def taper_verdicts(weekly_loads: dict[date, float], peak_load: float,
                   race_date: date, today: date | None = None) -> dict:
    """纯函数口径：各周计划负荷 / 峰值周 → 逐周档位与总评。

    weekly_loads: 该周周一 → 周计划负荷；没有数据的周如实标 no_plan。
    """
    today = today or date.today()
    race_week = week_start(race_date)
    peak_ok = peak_load >= _MIN_PEAK_LOAD
    weeks = []
    for back in range(_TAPER_WINDOW_WEEKS - 1, -1, -1):
        ws = race_week - timedelta(weeks=back)
        label = _WEEK_LABELS[back]
        band = TAPER_BANDS[back]
        load = weekly_loads.get(ws)
        entry = {"week_start": ws.isoformat(), "label": label,
                 "planned_load": None, "ratio": None, "band": list(band),
                 "verdict": "no_plan", "why": None}
        if load is None:
            entry["why"] = "计划未覆盖这一周"
        elif not peak_ok:
            entry["planned_load"] = round(load)
            entry["why"] = "计划峰值负荷过小，减量率没有参考意义"
        else:
            ratio = load / peak_load
            entry.update(planned_load=round(load), ratio=round(ratio, 2))
            if ratio > band[1]:
                entry["verdict"] = "high"
                entry["why"] = f"计划负荷为峰值的 {ratio * 100:.0f}%，高于推荐带 {band[0] * 100:.0f}-{band[1] * 100:.0f}%，减量不足"
            elif ratio < band[0]:
                entry["verdict"] = "low"
                entry["why"] = f"计划负荷只有峰值的 {ratio * 100:.0f}%，低于推荐带 {band[0] * 100:.0f}-{band[1] * 100:.0f}%，减量偏大（体能流失风险）"
            else:
                entry["verdict"] = "ok"
                entry["why"] = f"计划负荷为峰值的 {ratio * 100:.0f}%，落在推荐带 {band[0] * 100:.0f}-{band[1] * 100:.0f}% 内"
        weeks.append(entry)

    verdicts = [w["verdict"] for w in weeks]
    if "high" in verdicts:
        overall = "赛末减量不足：比赛周照常重负荷是赛前最大的可避免错误，按周下调"
    elif "low" in verdicts:
        overall = "减量偏大：保留 1-2 次短促质量课维持神经募集，避免比赛日腿软"
    elif all(v == "ok" for v in verdicts):
        overall = "减量路径符合推荐衰减带，照计划执行即可"
    else:
        overall = "计划尚未覆盖赛末几周，生成完整课表后可得减量对照"
    return {"race_date": race_date.isoformat(),
            "days_to_race": (race_date - today).days,
            "peak_week_load": round(peak_load),
            "weeks": weeks, "verdict": overall}


def taper_check(db: Session, athlete: models.Athlete) -> dict:
    """从当前有效计划装配减量对照；无计划/比赛超出 4 周窗口时如实 empty。"""
    plan = db.scalar(select(models.TrainingPlan).where(
        models.TrainingPlan.athlete_id == athlete.id,
        models.TrainingPlan.status == "active"))
    if not plan:
        return {"empty": True, "reason": "暂无有效训练计划"}
    race_date = plan.race_date
    today = date.today()
    if not race_date or not (today <= race_date <= today + timedelta(days=28)):
        days = (race_date - today).days if race_date else None
        reason = ("计划没有可用的比赛日期" if days is None
                  else "比赛日期已过去，减量对照只对未来的比赛有意义" if days < 0
                  else f"距离比赛还有 {days} 天，赛前 4 周内这里会出现减量对照")
        return {"empty": True, "reason": reason}

    # 全计划所有课（含已完成的）按周汇总：峰值周可能在过去，作为衰减基准
    rows = db.scalars(select(models.PlanWorkout).join(
        models.PlanWeek, models.PlanWorkout.week_id == models.PlanWeek.id).where(
        models.PlanWeek.plan_id == plan.id)).all()
    ad = {"max_hr": athlete.max_hr, "resting_hr": athlete.resting_hr}
    weekly: dict[date, float] = {}
    for wo in rows:
        ws = week_start(wo.date)
        weekly[ws] = weekly.get(ws, 0.0) + planned_workout_load(wo.session_type, wo.duration_min, ad)
    if not weekly:
        return {"empty": True, "reason": "计划里还没有课，先排课后才谈得上减量"}

    # 赛末周可能已部分开始（含已完成课，即真实执行记录）：不过滤过去周，照实评估
    return {"empty": False, **taper_verdicts(weekly, max(weekly.values()), race_date, today)}
