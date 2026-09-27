"""跑者档案 / 目标 / 日程（课表）。

档案读写都经过 ``services.profile`` 的解析链：
  · 读：先按最新数据刷新可解析字段（实测值跟随训练自动更新），再返回来源溯源；
  · 写：**留空 = 交给系统自动解析**，只有用户显式提交的字段才会被标记为「你填写的」。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..services import profile

router = APIRouter(prefix="/api/athlete", tags=["athlete"])

# 无法从训练数据推导的身份事实：首次建档必须由用户提供
_REQUIRED_ON_CREATE = {
    "sex": "性别", "birth_year": "出生年份", "height_cm": "身高", "weight_kg": "体重",
}

# 新建档案时还没有任何历史数据可查，解析只用本人属性（年龄/性别）推算
_NO_EVIDENCE = {"activities": [], "metrics": []}


def _first(db: Session) -> models.Athlete | None:
    return db.scalar(select(models.Athlete).order_by(models.Athlete.id).limit(1))


@router.get("")
def get_athlete(db: Session = Depends(get_db)):
    from ..data import build_prediction, runner_type_payload

    a = _first(db)
    if not a:
        return {"athlete": None, "goals": [], "schedule_slots": [], "runner_type": None,
                "profile": None}
    # 读路径即刷新：静息心率/体重/训练年限等状态字段跟随最新数据（有实测依据才覆盖）
    prof = profile.refresh(db, a, commit=True)
    pred = build_prediction(db, a.id)
    from ..data import snapshot_race_predictions
    snapshot_race_predictions(db, a.id, pred)   # 有未来比赛时留当日预测快照（幂等）
    goals = [_goal_dict(g, pred) for g in a.goals]
    return {
        "athlete": a,
        "goals": goals,
        "schedule_slots": a.schedule_slots,
        "runner_type": runner_type_payload(db, a, pred=pred),
        "current_vdot": pred.current_vdot,
        "profile": profile.payload(prof, changed=(a.profile_meta or {}).get("_refresh", {}).get("changed")),
    }


def _goal_dict(g: models.Goal, pred) -> dict:
    """序列化目标，并计算「目标进度 + 是否达成」：用当前预测成绩与目标成绩的比值。
    进度 = target / 当前预测（封顶 1.0），>=1 即达成。无预测成绩时 progress 为 None。"""
    progress, achieved = None, False
    if g.status == "active" and g.target_time_sec and pred:
        p = pred.predictions.get(g.race_type)
        if p and p.get("time_sec"):
            ratio = g.target_time_sec / p["time_sec"]
            progress = round(min(1.0, ratio), 4)
            achieved = ratio >= 1.0
    return {
        "id": g.id, "race_type": g.race_type, "target_time_sec": g.target_time_sec,
        "target_label": g.target_label, "target_date": g.target_date.isoformat() if g.target_date else None,
        "priority": g.priority, "status": g.status, "created_at": g.created_at.isoformat(),
        "progress": progress, "achieved": achieved,
    }


@router.put("")
def update_athlete(data: schemas.AthleteIn, db: Session = Depends(get_db)):
    """更新/创建档案。未传的字段保持现值不动；传 null 等同「交回系统自动解析」。"""
    provided = data.model_dump(exclude_unset=True)
    provided = {k: v for k, v in provided.items() if v is not None}

    a = _first(db)
    if not a:
        missing = [label for key, label in _REQUIRED_ON_CREATE.items() if key not in provided]
        if missing:
            raise HTTPException(
                422, "首次建档需要这些无法自动推导的信息：" + "、".join(missing))
        a = models.Athlete(**provided)
        # 新建时还没有任何历史数据，直接用空证据按本人属性推算，
        # 好在第一次 flush 之前就把 NOT NULL 字段填好——不能等入库后再解析，
        # 否则这一行会带着 NULL 落库并被约束拦下。
        prof = profile.refresh(db, a, force=True, commit=False, evidence=_NO_EVIDENCE)
    else:
        # 显式传 null 的可解析字段 = 「清空，交回系统算」
        for f in profile.AUTO_FIELDS:
            if f in data.model_fields_set and getattr(data, f) is None:
                profile.clear_user_marker(a, f)
        for k, v in provided.items():
            setattr(a, k, v)
        profile.mark_user_fields(a, list(provided))
        # 写入路径本就低频，无条件重新解析（force=True），保证返回值与库内一致
        prof = profile.refresh(db, a, force=True, commit=False)

    # 兜底：NOT NULL 列必须有值。解析链给不出（无实测、无年龄）时明确报错，
    # 而不是静默塞一个与本人无关的常数。
    for key in profile.AUTO_FIELDS:
        if getattr(a, key, None) is None:
            raise HTTPException(422, f"{key} 既未填写也无法按你的数据推导，请手动填写")

    db.add(a)          # 新建时入库；已存在的实例 add 是幂等的
    db.commit()
    db.refresh(a)
    return {"athlete": a, "profile": profile.payload(prof)}


@router.put("/auto")
def set_auto(data: schemas.AthleteAutoIn, db: Session = Depends(get_db)):
    """开关某字段的「自动推算」。

    关 = 钉死当前值不再跟随数据；开 = 连同「这是你手填的」标记一起清掉，
    让实测/推导数据立刻接管。
    """
    a = _first(db)
    if not a:
        raise HTTPException(404, "请先完善个人档案")
    if data.auto:
        profile.clear_user_marker(a, data.field)
    profile.set_locked(db, a, data.field, not data.auto, commit=False)
    prof = profile.refresh(db, a, force=True, commit=True)
    return {"field": data.field, "auto": data.auto, "profile": profile.payload(prof, athlete=a)}


@router.post("/goals")
def add_goal(data: schemas.GoalIn, db: Session = Depends(get_db)):
    a = _first(db)
    if not a:
        raise HTTPException(404, "请先完善个人档案")
    g = models.Goal(athlete_id=a.id, **data.model_dump())
    if not g.target_label:
        names = {"5k": "5公里", "10k": "10公里", "hm": "半程马拉松", "marathon": "全程马拉松"}
        from ..services.vdot import time_str
        g.target_label = f"{names[g.race_type]}{' ' + time_str(g.target_time_sec) if g.target_time_sec else ''}"
    db.add(g)
    db.commit()
    return g


@router.delete("/goals/{goal_id}")
def delete_goal(goal_id: int, db: Session = Depends(get_db)):
    g = db.get(models.Goal, goal_id)
    if not g:
        raise HTTPException(404, "目标不存在")
    # 清理引用该目标的进行中计划，避免悬空 goal_id（页面仍指向已删目标）
    for p in db.scalars(
            select(models.TrainingPlan).where(models.TrainingPlan.goal_id == goal_id)
            .where(models.TrainingPlan.status == "active")).all():
        p.status = "archived"
    db.delete(g)
    db.commit()
    return {"ok": True}


@router.post("/schedule")
def add_slot(data: schemas.WeeklySlotIn, db: Session = Depends(get_db)):
    a = _first(db)
    if not a:
        raise HTTPException(404, "请先完善个人档案")
    s = models.WeeklySlot(athlete_id=a.id, **data.model_dump())
    db.add(s)
    db.commit()
    return s


@router.delete("/schedule/{slot_id}")
def delete_slot(slot_id: int, db: Session = Depends(get_db)):
    s = db.get(models.WeeklySlot, slot_id)
    if not s:
        raise HTTPException(404, "时段不存在")
    db.delete(s)
    db.commit()
    return {"ok": True}
