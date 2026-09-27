"""活动记录查询/手动录入/单次活动详情。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..data import get_default_athlete
from ..db import get_db
from ..services.activity_detail import build_activity_detail
from ..services.load import estimate_load
from ..services.result_card import build_result_card
from .deps import require_athlete

router = APIRouter(prefix="/api/activities", tags=["activities"])


@router.get("")
def list_activities(limit: int = 100, offset: int = 0, sport: str = "",
                    date_from: str = "", date_to: str = "", db: Session = Depends(get_db)):
    q = select(models.Activity).order_by(models.Activity.start_time.desc())
    count_q = select(func.count(models.Activity.id))
    if sport:
        q = q.where(models.Activity.sport == sport)
        count_q = count_q.where(models.Activity.sport == sport)
    if date_from:
        q = q.where(models.Activity.start_time >= date_from)
        count_q = count_q.where(models.Activity.start_time >= date_from)
    if date_to:
        q = q.where(models.Activity.start_time <= date_to + " 23:59:59")
        count_q = count_q.where(models.Activity.start_time <= date_to + " 23:59:59")
    rows = db.scalars(q.offset(offset).limit(limit)).all()
    total = db.scalar(count_q) or 0
    return {"total": total, "items": [_activity_dict(r) for r in rows]}


@router.get("/{activity_id}/card")
def activity_card(activity_id: int, db: Session = Depends(get_db)):
    """即时成绩卡：对任意历史活动（含高驰/佳明同步）随时取完成反馈。"""
    act = db.get(models.Activity, activity_id)
    if not act:
        raise HTTPException(404, "活动不存在")
    return build_result_card(act, db)


@router.get("/{activity_id}")
def activity_detail(activity_id: int, db: Session = Depends(get_db)):
    act = db.get(models.Activity, activity_id)
    if not act:
        raise HTTPException(404, "活动不存在")
    athlete = get_default_athlete(db)
    return build_activity_detail(act, athlete)


@router.post("")
def add_activity(data: schemas.ActivityIn, db: Session = Depends(get_db)):
    athlete = require_athlete(db)
    r = models.Activity(athlete_id=athlete.id, platform="manual",
                        **data.model_dump())
    if r.gear_id and not db.get(models.Gear, r.gear_id):
        r.gear_id = None   # 装备不存在时静默忽略，避免脏外键
    if r.duration_sec and r.distance_m:
        r.effort_score = round(r.distance_m / r.duration_sec * 3600 / 1000, 2)
        r.calories = r.calories or _estimate_calories(
            r.sport, r.duration_sec, r.avg_hr, r.distance_m,
            weight_kg=athlete.weight_kg,
            age=date.today().year - athlete.birth_year if athlete.birth_year else None,
            sex=athlete.sex)
    # 负荷只依赖时长（力量课常无距离），且统一走 load 引擎；来源记入 raw 便于追溯
    load, source = estimate_load(r.duration_sec, r.avg_hr, r.rpe, athlete)
    r.training_load = load
    r.raw = {**(r.raw or {}), "training_load_source": source}
    db.add(r)
    db.commit()
    d = _activity_dict(r)
    d["record_card"] = build_result_card(r, db)
    return d


@router.put("/{activity_id}")
def update_activity(activity_id: int, data: schemas.ActivityUpdate, db: Session = Depends(get_db)):
    """修正已有活动（手动/同步来源均可）：部分更新，只改传入字段；
    时长/距离/心率变化后重算 effort_score、卡路里与训练负荷。"""
    act = db.get(models.Activity, activity_id)
    if not act:
        raise HTTPException(404, "活动不存在")
    updates = data.model_dump(exclude_unset=True)
    if "gear_id" in updates and updates["gear_id"] is not None \
            and not db.get(models.Gear, updates["gear_id"]):
        raise HTTPException(404, "装备不存在")
    athlete = get_default_athlete(db)
    for k, v in updates.items():
        setattr(act, k, v)
    if act.duration_sec and act.distance_m:
        act.effort_score = round(act.distance_m / act.duration_sec * 3600 / 1000, 2)
        if "calories" not in updates or updates["calories"] is None:
            act.calories = _estimate_calories(
                act.sport, act.duration_sec, act.avg_hr, act.distance_m,
                weight_kg=athlete.weight_kg if athlete else None,
                age=date.today().year - athlete.birth_year
                if athlete and athlete.birth_year else None,
                sex=athlete.sex if athlete else "male")
    load, source = estimate_load(act.duration_sec, act.avg_hr, act.rpe, athlete)
    act.training_load = load
    act.raw = {**(act.raw or {}), "training_load_source": source}
    db.commit()
    return {"ok": True, "activity": _activity_dict(act)}


@router.delete("/{activity_id}")
def delete_activity(activity_id: int, db: Session = Depends(get_db)):
    """删除错误/重复的活动；课表里指向它的「已完成」引用一并清空，
    装备里程是查询时实时聚合的，无需重算。"""
    act = db.get(models.Activity, activity_id)
    if not act:
        raise HTTPException(404, "活动不存在")
    for w in db.scalars(select(models.PlanWorkout)
                        .where(models.PlanWorkout.completed_activity_id == activity_id)).all():
        w.completed_activity_id = None
        if w.status == "completed":
            w.status = "planned"
    db.delete(act)
    db.commit()
    return {"ok": True}


@router.post("/{activity_id}/gear")
def assign_gear(activity_id: int, data: schemas.GearAssignIn, db: Session = Depends(get_db)):
    """给已有活动分配/取消装备（跑鞋里程统计的入口）。"""
    act = db.get(models.Activity, activity_id)
    if not act:
        raise HTTPException(404, "活动不存在")
    if data.gear_id is not None and not db.get(models.Gear, data.gear_id):
        raise HTTPException(404, "装备不存在")
    act.gear_id = data.gear_id
    db.commit()
    return {"ok": True, "gear_id": act.gear_id}


def _estimate_calories(sport: str, duration_sec: int, avg_hr: int | None, distance_m: float,
                       weight_kg: float = 65, age: int | None = None,
                       sex: str = "male") -> int:
    """无设备卡路里时的估算：有心率+年龄走 Keytel 心率公式，否则 MET 法。

    体重取档案真实值（未传/未建档回退 65 kg）；Keytel 按性别取系数
    （Keytel et al. 2005，kJ/min → kcal）。
    """
    minutes = duration_sec / 60
    if avg_hr and age:
        if sex == "female":
            kj_min = -20.4022 + 0.4472 * avg_hr + 0.1263 * weight_kg + 0.074 * age
        else:
            kj_min = -55.0969 + 0.6309 * avg_hr + 0.1988 * weight_kg + 0.2017 * age
        return int(kj_min / 4.184 * minutes)
    if sport == "run" and distance_m and duration_sec:
        speed_kmh = distance_m / 1000 / (duration_sec / 3600)
        met = 1.0 if speed_kmh < 6 else speed_kmh * 1.036  # ACSM 走/跑 MET 近似
    elif sport == "ride":
        met = 8.0
    elif sport == "swim":
        met = 7.0
    elif sport == "strength":
        met = 5.0
    else:
        met = 3.5
    return int(met * weight_kg * (minutes / 60))


def _activity_dict(r: models.Activity) -> dict:
    pace = None
    if r.distance_m and r.duration_sec:
        pace = round(r.duration_sec / (r.distance_m / 1000))
    return {
        "id": r.id, "sport": r.sport, "title": r.title, "platform": r.platform,
        "start_time": r.start_time.isoformat(), "duration_sec": r.duration_sec,
        "distance_m": r.distance_m, "avg_hr": r.avg_hr, "max_hr": r.max_hr,
        "avg_cadence": r.avg_cadence, "pace_sec_per_km": pace,
        "elevation_m": r.elevation_m or 0, "calories": r.calories,
        "avg_power": r.avg_power, "rpe": r.rpe,
        "te_aerobic": r.te_aerobic, "te_anaerobic": r.te_anaerobic,
        "training_load": r.training_load,
        "gear_id": r.gear_id, "gear_name": r.gear.name if r.gear else None,
    }
