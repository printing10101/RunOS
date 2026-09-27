"""训练计划：生成 / 查看 / 下发到高驰·佳明手表 / FIT 导出 / 完成打卡。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..data import (
    active_plan,
    archive_plan,
    athlete_dict,
    build_evaluation,
    get_default_athlete,
)
from ..db import get_db
from ..integrations.base import IntegrationError
from ..services import planner
from ..services.vdot import time_str
from .deps import require_athlete

router = APIRouter(prefix="/api/plan", tags=["plan"])


@router.post("/generate")
def generate_plan(data: schemas.PlanGenerateIn, db: Session = Depends(get_db)):
    athlete = require_athlete(db)
    goal = db.get(models.Goal, data.goal_id)
    if not goal:
        raise HTTPException(404, "目标不存在")
    return _create_plan_for_goal(db, athlete, goal, data.start_date, data.weekly_km_peak, data.plan_name)


def _create_plan_for_goal(db: Session, athlete: models.Athlete, goal: models.Goal,
                          start_date: date | None = None,
                          weekly_km_peak: float | None = None,
                          plan_name: str = "") -> dict:
    """按目标生成周期化计划并落库（plans 路由与 AI 自由文本建计划共用）。

    VDOT 与天赋响应速度一律来自真实评估，数据不足时拒绝生成（不使用任何默认值）。
    """
    ev = build_evaluation(db, athlete)
    current_vdot = ev["current_vdot"]
    talent = (ev["dimensions"].get("talent") or {}).get("score")
    if not current_vdot:
        raise HTTPException(400, "暂无足够训练数据估算当前 VDOT，请先同步/录入近 4 周以上的跑步记录（含一次尽努力的中长距离）")
    if not talent:
        raise HTTPException(400, "训练数据不足以评估天赋响应速度，请先录入更多训练记录后再生成计划")
    slots = db.scalars(select(models.WeeklySlot).where(
        models.WeeklySlot.athlete_id == athlete.id, models.WeeklySlot.kind == "available")).all()
    slots_d = [{"weekday": s.weekday, "start_time": s.start_time,
                "duration_minutes": s.duration_minutes} for s in slots]
    if not slots_d:
        raise HTTPException(400, "请先在「日程管理」中添加至少一个可训练时段")
    try:
        plan = planner.generate_plan(
            athlete_dict(athlete) | {"id": athlete.id},
            {"race_type": goal.race_type, "target_time_sec": goal.target_time_sec,
             "target_label": goal.target_label, "target_date": goal.target_date,
             "start_date": start_date,
             "weekly_km_peak": weekly_km_peak},
            current_vdot=current_vdot,
            talent_score=talent,
            weekly_km_now=ev["weekly_km_avg"] or 0,
            available_slots=slots_d,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    # 旧计划归档（同时清掉其未执行课次，避免作废课串进「今日训练」等按运动员查的链路），写入新计划
    for old in db.scalars(select(models.TrainingPlan).where(models.TrainingPlan.status == "active")).all():
        archive_plan(db, old)
    row = models.TrainingPlan(
        athlete_id=athlete.id, goal_id=goal.id, name=plan_name or plan["name"],
        race_type=plan["race_type"], target_time_sec=plan["target_time_sec"],
        start_date=date.fromisoformat(plan["start_date"]),
        race_date=date.fromisoformat(plan["race_date"]),
        weekly_km_peak=plan["weekly_km_peak"], feasibility=plan["feasibility"],
    )
    db.add(row)
    db.flush()
    for w in plan["weeks"]:
        pw = models.PlanWeek(
            plan_id=row.id, week_index=w["week_index"],
            start_date=date.fromisoformat(w["start_date"]),
            phase=w["phase"], phase_note=w["phase_note"], focus=w["focus"],
            target_km=w["target_km"],
        )
        db.add(pw)
        db.flush()
        for wo in w["workouts"]:
            db.add(models.PlanWorkout(
                week_id=pw.id, athlete_id=athlete.id,
                date=date.fromisoformat(wo["date"]), start_time=wo["start_time"],
                session_type=wo["session_type"], title=wo["title"],
                description=wo.get("description", ""),
                distance_km=wo["distance_km"], duration_min=wo["duration_min"],
                structured=wo["structured"], diet_tip=wo.get("diet_tip", ""),
            ))
    db.commit()
    return {"ok": True, "plan_id": row.id, "feasibility": plan["feasibility"]}


@router.get("/current")
def current_plan(db: Session = Depends(get_db)):
    plan = active_plan(db)
    if not plan:
        return None
    return _plan_dict(plan)


@router.get("/calendar.ics")
def calendar_ics(db: Session = Depends(get_db)):
    """把当前有效计划的逐日课表导出为日历订阅文件（可导入系统日历/日历 App 订阅）。

    注意：本路由必须注册在 /{plan_id} 之前，否则会被路径参数吞掉导致 422。
    """
    from ..export.ics_workout import build_plan_ics

    plan = active_plan(db)
    if not plan:
        raise HTTPException(404, "暂无有效训练计划")
    content = build_plan_ics(plan)
    filename = f"training_plan_{plan.id}.ics"
    return Response(
        content=content,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{plan_id}")
def get_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(models.TrainingPlan, plan_id)
    if not plan:
        raise HTTPException(404, "计划不存在")
    return _plan_dict(plan)


@router.post("/workouts/{workout_id}/push")
def push_workout(workout_id: int, data: schemas.PushIn, db: Session = Depends(get_db)):
    """把结构化训练下发到所选平台（佳明/Strava），供手表同步。

    逐平台尽力而为：单个平台失败（未连接/接口拒绝）不影响其余平台；
    重试时已成功的平台按 pushed_platforms 跳过，避免在平台侧重复建训练。
    """
    wo = db.get(models.PlanWorkout, workout_id)
    if not wo:
        raise HTTPException(404, "训练课不存在")
    if not wo.structured:
        raise HTTPException(400, "该训练课没有结构化步骤，无法下发")

    pushed = list(wo.pushed_platforms or [])
    athlete = get_default_athlete(db)
    max_hr = getattr(athlete, "max_hr", None)
    results = []
    external_ids = []
    ok_count = 0
    for platform in data.platforms:
        if platform in pushed:
            results.append({"platform": platform, "ok": True, "skipped": True,
                            "message": "此前已下发成功，已跳过（如需重发请先撤销记录）"})
            continue
        conn = db.scalar(select(models.PlatformConnection).where(
            models.PlatformConnection.platform == platform, models.PlatformConnection.status == "connected"))
        if not conn:
            results.append({"platform": platform, "ok": False,
                            "error": f"{platform} 尚未连接，请先到「平台连接」页绑定"})
            continue
        from ..routers.connections import get_adapter
        adapter = get_adapter(conn)
        try:
            wid = adapter.push_workout(wo.title, wo.structured, sport="run", max_hr=max_hr)
        except IntegrationError as e:
            results.append({"platform": platform, "ok": False, "error": str(e)})
            continue
        external_ids.append({"platform": platform, "workout_id": wid})
        pushed.append(platform)
        results.append({"platform": platform, "ok": True, "workout_id": wid})
        ok_count += 1

    if ok_count:
        wo.pushed_platforms = pushed
        wo.status = "synced"
        wo.external_workout_id = ",".join(e["workout_id"] for e in external_ids if e["workout_id"])
        db.commit()
    return {"ok": ok_count > 0, "pushed": pushed, "external": external_ids, "results": results}


@router.get("/workouts/{workout_id}/fit")
def download_fit(workout_id: int, db: Session = Depends(get_db)):
    """导出 .fit 结构化训练文件（可手动导入高驰/佳明 App）。"""
    from ..export.fit_workout import build_workout_fit
    wo = db.get(models.PlanWorkout, workout_id)
    if not wo or not wo.structured:
        raise HTTPException(404, "训练课不存在或无结构化步骤")
    athlete = require_athlete(db)
    data = build_workout_fit(wo.title, wo.structured, max_hr=athlete.max_hr)
    filename = f"workout_{wo.date.isoformat()}_{wo.id}.fit"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/workouts/{workout_id}/complete")
def complete_workout(workout_id: int, data: schemas.PlanWorkoutCompleteIn, db: Session = Depends(get_db)):
    wo = db.get(models.PlanWorkout, workout_id)
    if not wo:
        raise HTTPException(404, "训练课不存在")
    if data.activity_id is not None and not db.get(models.Activity, data.activity_id):
        raise HTTPException(404, "关联的活动不存在")
    if data.completed:
        wo.status = "completed"
        # 记录完成本节课对应的实际活动，使课表与真实训练可对账
        wo.completed_activity_id = data.activity_id
    else:
        wo.status = "planned"
        wo.completed_activity_id = None
    db.commit()
    # 训练后 AI 点评：必须在 commit 之后拉起，后台线程用独立会话读取，
    # 提交前启动会读到旧的 planned 状态而跳过
    if data.completed:
        from ..services.coach_comment import regenerate_async
        regenerate_async(wo.id)
    return {"ok": True, "status": wo.status, "completed_activity_id": wo.completed_activity_id}


def _plan_dict(plan: models.TrainingPlan) -> dict:
    return {
        "id": plan.id, "name": plan.name, "race_type": plan.race_type,
        "target_time_sec": plan.target_time_sec,
        "target_time_str": time_str(plan.target_time_sec) if plan.target_time_sec else None,
        "start_date": plan.start_date.isoformat(), "race_date": plan.race_date.isoformat(),
        "weekly_km_peak": plan.weekly_km_peak, "feasibility": plan.feasibility,
        "status": plan.status, "source": plan.source,
        "weeks": [{
            "id": w.id, "week_index": w.week_index, "start_date": w.start_date.isoformat(),
            "phase": w.phase, "phase_note": w.phase_note, "focus": w.focus, "target_km": w.target_km,
            "workouts": [{
                "id": wo.id, "date": wo.date.isoformat(), "start_time": wo.start_time,
                "session_type": wo.session_type, "title": wo.title,
                "distance_km": wo.distance_km, "duration_min": wo.duration_min,
                "structured": wo.structured, "diet_tip": wo.diet_tip,
                "status": wo.status, "pushed_platforms": wo.pushed_platforms or [],
                "coach_comment": wo.coach_comment or "",
                "coach_comment_at": wo.coach_comment_at.isoformat() if wo.coach_comment_at else None,
            } for wo in w.workouts],
        } for w in plan.weeks],
    }
