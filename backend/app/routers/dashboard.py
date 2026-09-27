"""总览仪表盘聚合数据。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..data import (
    active_plan,
    activities_dicts,
    body_metrics_dicts,
    build_prediction,
    build_training_status_for,
    get_default_athlete,
    runner_type_payload,
)
from ..db import get_db
from ..services.evaluator import weekly_km_series
from ..services.vdot import daniels_paces

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/recap-weekly")
def weekly_recap(db: Session = Depends(get_db)):
    """每周自动复盘：本周跑量/强度分布/恢复趋势 + 下周计划预览（跑后环节）。"""
    from ..services.weekly_recap import build_weekly_recap
    return build_weekly_recap(db)


@router.get("")
def dashboard(db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True}
    acts = activities_dicts(db, athlete.id, days=190)
    pred = build_prediction(db, athlete.id)
    from ..data import snapshot_race_predictions
    snapshot_race_predictions(db, athlete.id, pred)   # 有未来比赛时留当日预测快照（幂等）

    # 近 12 周跑量/负荷
    km_series = weekly_km_series(acts, 12)

    this_week = [a for a in acts if a["start_time"] >= datetime.now() - timedelta(days=7)]
    last_snap = db.scalar(select(models.Assessment).order_by(models.Assessment.id.desc()).limit(1))
    plan = active_plan(db)
    next_workout = None
    plan_week = None
    if plan:
        today = date.today()
        pw = db.scalars(select(models.PlanWeek).where(
            models.PlanWeek.plan_id == plan.id,
            models.PlanWeek.start_date <= today).order_by(models.PlanWeek.start_date.desc())).first()
        if pw and today <= pw.start_date + timedelta(days=6):
            plan_week = {"week_index": pw.week_index, "phase": pw.phase,
                         "target_km": pw.target_km, "focus": pw.focus}
        wo = db.scalars(select(models.PlanWorkout).where(
            models.PlanWorkout.athlete_id == athlete.id,
            models.PlanWorkout.status.in_(["planned", "synced"]),
            models.PlanWorkout.date >= date.today()).order_by(models.PlanWorkout.date)).first()
        if wo:
            next_workout = {
                "id": wo.id, "date": wo.date.isoformat(), "title": wo.title,
                "session_type": wo.session_type, "distance_km": wo.distance_km,
                "duration_min": wo.duration_min, "pushed_platforms": wo.pushed_platforms or [],
                "start_time": wo.start_time,
                "structured": wo.structured[:4] if wo.structured else [],
                "diet_tip": wo.diet_tip,
            }

    marathon = pred.predictions.get("marathon")
    return {
        "athlete": {"name": athlete.name, "age": date.today().year - athlete.birth_year,
                    "sex": athlete.sex, "weight_kg": athlete.weight_kg},
        "runner_type": runner_type_payload(db, athlete, pred=pred, acts=acts),
        "weekly_km_series": km_series,
        "this_week": {"sessions": len(this_week),
                      "km": round(sum(a["distance_m"] for a in this_week) / 1000, 1),
                      "hours": round(sum(a["duration_sec"] for a in this_week) / 3600, 1)},
        "current_vdot": pred.current_vdot,
        "paces": daniels_paces(pred.current_vdot) if pred.current_vdot else [],
        "marathon_prediction": marathon and {"time_str": marathon["time_str"], "pace": marathon["pace"]},
        "assessment": last_snap and {"total_score": last_snap.total_score, "grade": last_snap.grade,
                                     "percentile": last_snap.percentile},
        "next_workout": next_workout,
        "plan_week": plan_week,
        "plan": plan and {"id": plan.id, "name": plan.name,
                          "start_date": plan.start_date.isoformat(),
                          "race_date": plan.race_date.isoformat()},
        "recent_activities": [{
            "id": a["id"], "title": a["title"], "sport": a["sport"],
            "start_time": a["start_time"].isoformat(),
            "distance_km": round(a["distance_m"] / 1000, 1),
            "duration_min": round(a["duration_sec"] / 60),
            "avg_hr": a["avg_hr"], "kind": a["kind"],
            "pace_sec_per_km": round(a["duration_sec"] / (a["distance_m"] / 1000)) if a["distance_m"] and a["duration_sec"] else None,
            "elevation_m": a.get("elevation_m", 0),
        } for a in sorted(acts, key=lambda x: x["start_time"], reverse=True)[:8]],
        "body_trend": body_metrics_dicts(db, athlete.id, days=30),
        "status_summary": _status_summary(db, athlete, acts),
    }


def _status_summary(db: Session, athlete: models.Athlete, acts: list[dict]) -> dict | None:
    """总览页顶部状态条：训练状态标签 + 准备度 + 恢复时间（含官方恢复快照）。"""
    if not acts:
        return None
    st, _ = build_training_status_for(db, athlete, acts)
    out = {
        "status": st["status"]["label"], "status_detail": st["status"]["detail"],
        "readiness": st["readiness"].get("score"),
        "readiness_verdict": st["readiness"].get("verdict"),
        "recovery_time_h": st["recovery_time_h"],
        "acwr": st["acwr"],
    }
    # 高驰官方恢复状态（当日 body metric 里的官方快照，与引擎估算互为对照）
    today = date.today()
    metric = db.scalar(select(models.BodyMetric).where(
        models.BodyMetric.athlete_id == athlete.id, models.BodyMetric.date == today))
    if metric and metric.recovery_pct is not None:
        out["official_recovery"] = {"pct": metric.recovery_pct,
                                    "level": metric.recovery_level or ""}
    return out
