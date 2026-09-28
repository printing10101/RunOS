"""每周自动复盘（跑后环节，借鉴 Runkeeper / adidas Running 的周度循环）。

引擎规则生成、确定性、不依赖 LLM：给出一周跑量 / 强度分布 / 恢复趋势 的
结构化小结 + 下周计划预览。全程「不指责」：即便本周不理想也只做客观归因与
正面收尾，不做负面评判。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from .. import models
from ..data import active_plan, activities_dicts, body_metrics_dicts, get_default_athlete
from . import load_status
from .evaluator import vdot_trend


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def build_weekly_recap(db) -> dict:
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True}

    acts = activities_dicts(db, athlete.id, days=120)
    now = datetime.now()
    this_start = _week_start(now.date())
    prev_start = this_start - timedelta(days=7)
    this_end = this_start + timedelta(days=7)

    this = [a for a in acts if this_start <= a["start_time"].date() < this_end]
    prev = [a for a in acts if prev_start <= a["start_time"].date() < this_start]

    # 负荷 / 恢复 / 单调性 / ACWR：复用训练状态引擎
    metrics = body_metrics_dicts(db, athlete.id, days=60)
    ad = {"max_hr": athlete.max_hr, "resting_hr": athlete.resting_hr,
          "hrv_baseline": athlete.hrv_baseline, "weight_kg": athlete.weight_kg}
    plan = active_plan(db)
    race_date = plan.race_date if plan else None
    trend = vdot_trend(acts, date.today().year - athlete.birth_year, athlete.sex)
    st = load_status.build_training_status(acts, ad, metrics, race_date=race_date, vdot_trend=trend)

    # 本周 / 上周 强度分布（80/20：hard 约 = 有氧偏上 + 无氧）
    def _split(items):
        hard_km = sum(a["distance_m"] / 1000 for a in items
                      if load_status.classify_intensity(a.get("avg_hr"), a.get("sport"), ad) in ("anaerobic", "high_aerobic"))
        km = sum(a["distance_m"] / 1000 for a in items if a.get("sport") == "run")
        return {"km": round(km, 1), "hard_km": round(hard_km, 1),
                "hard_pct": round(hard_km / km * 100) if km > 0 else 0}

    this_s, prev_s = _split(this), _split(prev)

    def _stats(items):
        return {"sessions": len(items),
                "km": round(sum(a["distance_m"] / 1000 for a in items), 1),
                "load": round(sum(a.get("training_load") or 0 for a in items)),
                "hours": round(sum(a["duration_sec"] for a in items) / 3600, 1)}

    # 下周计划预览
    next_week = None
    pw = db.scalars(select(models.PlanWeek).where(
        models.PlanWeek.start_date >= this_end).order_by(models.PlanWeek.start_date)).first()
    if pw:
        wos = db.scalars(select(models.PlanWorkout).where(
            models.PlanWorkout.week_id == pw.id).order_by(models.PlanWorkout.date)).all()
        next_week = {
            "phase": pw.phase, "week_index": pw.week_index,
            "phase_note": pw.phase_note, "target_km": pw.target_km, "focus": pw.focus,
            "count": len(wos),
            "titles": [{"date": w.date.isoformat(), "title": w.title, "session_type": w.session_type}
                       for w in wos],
        }

    return {
        "week": {"key": f"W{this_start.isocalendar().week}", "range": f"{this_start.month}/{this_start.day}–{this_start.day + 6}"},
        "this_week": _stats(this),
        "prev_week": _stats(prev),
        "split": {"this": this_s, "prev": prev_s},
        "load": {"acute": st["acute_7d"], "chronic": st["chronic_weekly"], "acwr": st["acwr"],
                 "monotony": st["monotony"], "fitness": st["fitness"],
                 "fatigue": st["fatigue"], "form": st["form"]},
        "recovery": {"readiness": st["readiness"].get("score"), "recovery_h": st["recovery_time_h"],
                     "status": st["status"]["label"], "status_detail": st["status"]["detail"]},
        "next": next_week,
        "engine": _engine_decision_summary(db, this_start),
        "lead": _lead_line(this, this_s, prev_s, st, next_week),
    }


def _engine_decision_summary(db, week_start: date) -> dict:
    """本周引擎决策摘要：漂移引擎建议的采纳/忽略情况（阶段 5.3 并入周复盘）。"""
    from .proposal_store import week_decision_summary

    try:
        return week_decision_summary(db, week_start)
    except Exception:
        # 摘要失败不影响复盘主体（ai_proposals 表可能尚不存在于极老库）
        return {"applied": 0, "dismissed": 0, "withdrawn": 0, "applied_titles": []}


def _lead_line(this: list[dict], this_s: dict, prev_s: dict, st: dict, next_week: dict | None) -> str:
    """一句正面收尾的主线文案（不指责：只客观归因 + 向前带一步）。"""
    n = len(this)
    t = this_s.get("km", 0)
    hard = this_s.get("hard_pct", 0)
    parts = []
    if n:
        parts.append(f"本周完成 {n} 次训练、{t} km，其中强度课占 {hard}%")
    else:
        parts.append("本周还没有训练记录")
    # 强度分布提示（80/20）
    if n and hard > 0:
        if hard < 20:
            parts.append(f"强度占比 {hard}% 略低，下周可把质量课保持住")
        elif hard > 30:
            parts.append(f"强度占比 {hard}% 略高，注意用轻松日把节奏拉回来")
        else:
            parts.append("强度节奏合理，符合 80/20 原则")
    # 恢复与状态
    st_ = st["status"]["label"]
    parts.append(f"训练状态「{st_}」")
    # 下周
    if next_week:
        parts.append(f"下周是第 {next_week['week_index']} 周、{({'base': '基础期', 'build': '强化期', 'peak': '巅峰期', 'taper': '减量期'}).get(next_week['phase'], next_week['phase'])}，目标 {next_week['target_km']} km")
        if next_week.get("focus"):
            parts.append(f"重点：{next_week['focus']}")
    return " · ".join(parts) + "，继续稳稳推进"